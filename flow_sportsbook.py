"""
flow_sportsbook.py — ATLAS · Flow Module · Sportsbook v5.0
─────────────────────────────────────────────────────────────────────────────
ODDS ENGINE v5 — Elo-Based Power Rankings
  · _compute_elo_ratings(): processes tsl_history.db games chronologically,
    builds per-owner Elo ratings with margin-of-victory multiplier,
    K-factor scaling, and season regression. Filters out CPU games.
  · _team_quality_score(): current team quality from OVR + off/def ranks
  · _combined_power(): 80% owner Elo + 20% team quality → 0-100 power score
  · _calc_spread(): (away_power - home_power) / scaling_factor, HFE only
    when home is already favored, capped ±21.0
  · _spread_to_ml(): spread → American ML odds (extended table)
  · _calc_ou(): owner historical avg pts ± rank adjustments, ceiling 99.5
  · _build_game_lines(): admin line_overrides applied first, engine fallback

ADMIN MANAGEMENT (via /commish sb subcommands — _impl methods live here):
  status, lines, setspread, setml, setou, lock, lockall, unlockall,
  cancelgame, refund, balance, resetlines, addprop, settleprop

USER COMMANDS:
  /sportsbook   — Unified hub: TSL + NFL/NBA/MLB/NHL + My Bets/History/Leaderboard/Props

CHANGES v5.0 vs v4.0:
  BREAK ENTIRE odds engine replaced with Elo-based power rankings
  BREAK spread sign convention FIXED (was inverted — wrong team was favored)
  BREAK HOME_FIELD_EDGE now conditional (only applied when home is favored)
  BREAK SPREAD_CAP raised 14.5 → 21.0 (Madden has wider margins)
  BREAK O/U ceiling raised 72.0 → 99.5 (high-scoring Madden games hit 90+)
  BREAK O/U multipliers boosted (off: 0.28→0.35, def: 0.18→0.22)
  ADD  _compute_elo_ratings() — full Elo system from tsl_history.db
  ADD  _team_quality_score() — OVR + off/def rank composite
  ADD  _combined_power() — 80% owner Elo + 20% team quality
  ADD  _safe_float()/_safe_int() — fixes Python `or` falsiness bug where
       0.0 win% or 0 rank was silently replaced with defaults
  FIX  CPU games filtered from Elo history (98 games were inflating stats)
  FIX  Only status='3' (final) games used in Elo computation
  FIX  ML table extended for spreads up to 21+ points
  KEEP all admin commands, UI components, grading logic unchanged
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import data_manager as dm
import flow_wallet
from real_sportsbook_cog import (
    SPORT_EMOJI, SUPPORTED_SPORTS as REAL_SPORTS,
    _parse_commence, _place_real_bet, _short_name as _real_short_name,
    CustomRealWagerModal,
)
from sportsbook_cards import (
    build_sportsbook_card, build_stats_card, build_parlay_analytics_card,
    build_match_detail_card, build_real_match_detail_card, card_to_file,
)
from atlas_send import send_card
from flow_wallet import get_theme_for_render
from db_migration_snapshots import setup_snapshots_table, take_daily_snapshot, backfill_from_bets
import logging

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
_DIR              = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
HISTORY_DB_PATH   = os.path.join(_DIR, "tsl_history.db")

from atlas_colors import AtlasColors
TSL_GOLD          = AtlasColors.TSL_GOLD.value
TSL_BLACK         = 0x1A1A1A
TSL_RED           = AtlasColors.ERROR.value
TSL_GREEN         = AtlasColors.SUCCESS.value

ADMIN_ROLE_NAME   = "Commissioner"
from permissions import ADMIN_USER_IDS

STARTING_BALANCE  = 1000
MIN_BET           = 50      # unified with real sportsbook
WAGER_PRESETS     = [50, 100, 250, 500, 1000]
MAX_PARLAY_LEGS   = 6
HOME_FIELD_EDGE   = 2.0       # pts advantage — only applied when home is already favored
SPREAD_CAP        = 21.0      # max absolute spread value (Madden has wider margins)
OU_FLOOR          = 35.0      # minimum O/U total
OU_CEILING        = 99.5      # maximum O/U total (Madden games can hit 90+)
_DB_TIMEOUT       = 10
MAX_PAYOUT        = 10_000_000  # sanity cap — no single payout should exceed 10M

# Elo system constants
ELO_INITIAL       = 1500
ELO_K_NEW         = 32        # K-factor for owners with < 20 games
ELO_K_MID         = 24        # K-factor for owners with 20-50 games
ELO_K_EST         = 20        # K-factor for owners with 50+ games
ELO_SEASON_REGRESS = 0.75     # regress 25% toward 1500 each new season
ELO_OWNER_WEIGHT  = 0.80      # owner skill weight in combined power
ELO_TEAM_WEIGHT   = 0.20      # team quality weight in combined power
SPREAD_SCALING    = 4.0       # divisor: Elo-based power diff → spread points

SPORTSBOOK_VERSION = "v5.0"
log.info(f"[SPORTSBOOK] Loading {SPORTSBOOK_VERSION}")

# ── Autograde health tracking ────────────────────────────────────────────────
_autograde_health: dict = {
    "last_run_at": None,
    "last_run_duration_s": 0.0,
    "last_run_settled": 0,
    "last_run_skipped": 0,
    "total_runs": 0,
    "total_settled": 0,
    "consecutive_failures": 0,
    "task_alive": False,
}


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
                legs          TEXT,              -- DEPRECATED: no longer written (v4.4.0), use parlay_legs table
                combined_odds INTEGER,
                wager_amount  INTEGER,
                status        TEXT DEFAULT 'Pending',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS parlay_legs (
                leg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                parlay_id  TEXT    NOT NULL REFERENCES parlays_table(parlay_id),
                leg_index  INTEGER NOT NULL,
                game_id    TEXT    NOT NULL,
                matchup    TEXT    NOT NULL,
                pick       TEXT    NOT NULL,
                bet_type   TEXT    NOT NULL,
                line       REAL    NOT NULL DEFAULT 0,
                odds       INTEGER NOT NULL,
                source     TEXT    NOT NULL DEFAULT 'TSL',
                status     TEXT    NOT NULL DEFAULT 'Pending',
                UNIQUE(parlay_id, leg_index)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay  ON parlay_legs(parlay_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_game    ON parlay_legs(game_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_matchup ON parlay_legs(matchup)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_type    ON parlay_legs(bet_type, status)")

        # ── Parlay Cart (DB-backed, survives restarts) ─────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS parlay_cart (
                cart_leg_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id   INTEGER NOT NULL,
                source       TEXT    NOT NULL DEFAULT 'TSL',
                event_id     TEXT    NOT NULL,
                display      TEXT    NOT NULL,
                pick         TEXT    NOT NULL,
                bet_type     TEXT    NOT NULL,
                line         REAL    NOT NULL DEFAULT 0,
                odds         INTEGER NOT NULL,
                added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cart_user_event
            ON parlay_cart(discord_id, source, event_id)
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
            "ALTER TABLE parlay_legs ADD COLUMN source TEXT NOT NULL DEFAULT 'TSL'",
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
                log.info(f"[SB] Migrated {len(old_rows)} ou_line entries → line_overrides")
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  DB HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_balance(uid: int) -> int:
    return flow_wallet.get_balance_sync(uid)


def _update_balance(uid: int, delta: int, con=None, *,
                    subsystem=None, subsystem_id=None, reference_key=None) -> int:
    return flow_wallet.update_balance_sync(
        uid, delta, source="TSL_BET",
        reference_key=reference_key,
        con=con, subsystem=subsystem, subsystem_id=subsystem_id,
    )


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


_ALLOWED_LINE_COLS = {"home_spread", "away_spread", "home_ml", "away_ml", "ou_line"}


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
            if col not in _ALLOWED_LINE_COLS:
                continue
            # col is now guaranteed to be one of the 5 allowed literals
            con.execute(
                f"UPDATE line_overrides SET {col}=?, set_by=?, set_at=CURRENT_TIMESTAMP "
                f"WHERE game_id=?",
                (val, set_by, game_id)
            )


def _clear_line_overrides_for_week(week: int):
    """Remove line overrides for games in the given week."""
    # Resolve game_ids for this week from the live DataFrame (games_state has no week column).
    # weekIndex in the API is 0-based; `week` here matches the bet_week convention (1-based).
    src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
    if src.empty:
        log.warning("[SB] No game data loaded — cannot scope override clear to week %d", week)
        return

    df = src.copy()
    if "weekIndex" in df.columns:
        df["weekIndex"] = df["weekIndex"].apply(
            lambda x: int(float(x)) if x not in (None, "", "nan") else -1
        )
        week_games = df[df["weekIndex"] == (week - 1)]
    elif "week" in df.columns:
        week_games = df[df["week"].apply(
            lambda x: int(float(x)) if x not in (None, "") else -1
        ) == week]
    else:
        log.warning("[SB] No week column found — cannot scope override clear to week %d", week)
        return

    game_ids = [
        str(row.get("gameId", row.get("id", row.get("matchup_key", ""))))
        for row in week_games.to_dict("records")
        if row.get("gameId") or row.get("id") or row.get("matchup_key")
    ]

    if not game_ids:
        log.debug("[SB] No game_ids found for week %d — nothing to clear", week)
        return

    with _db_con() as con:
        placeholders = ",".join("?" * len(game_ids))
        con.execute(
            f"DELETE FROM line_overrides WHERE game_id IN ({placeholders})",
            game_ids,
        )
    log.debug("[SB] Cleared line overrides for week %d (%d games)", week, len(game_ids))


# ═════════════════════════════════════════════════════════════════════════════
#  ODDS ENGINE v5 — Elo-Based Power Rankings
# ═════════════════════════════════════════════════════════════════════════════

_ELO_CACHE: dict          = {}     # { userName: elo_rating }
_OWNER_SCORING_CACHE: dict = {}    # { userName: { avg_scored, avg_allowed, games } }
_LEAGUE_AVG_SCORE: float  = 30.0   # per-team avg, calibrated from history DB
_MIDPOINT_RANK            = 16.5   # midpoint of 1–32


def _invalidate_elo_cache():
    global _ELO_CACHE, _OWNER_SCORING_CACHE
    _ELO_CACHE = {}
    _OWNER_SCORING_CACHE = {}


def _compute_elo_ratings() -> dict:
    """
    Build Elo ratings for all owners from tsl_history.db.

    Processes every completed game chronologically, updating owner Elo after each.
    Applies season regression (25% toward 1500) at each season boundary.

    Filters:
      - Only status='3' (final) games
      - Excludes CPU games and games with empty owners

    Returns { userName: elo_float } and populates _OWNER_SCORING_CACHE as side-effect.
    """
    global _ELO_CACHE, _OWNER_SCORING_CACHE, _LEAGUE_AVG_SCORE
    if _ELO_CACHE:
        return _ELO_CACHE

    elo: dict[str, float]  = {}    # userName → current Elo
    games_played: dict[str, int] = {}  # userName → total games processed
    scoring: dict[str, dict] = {}  # userName → { pts_scored, pts_allowed, games }

    try:
        con = sqlite3.connect(HISTORY_DB_PATH, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")

        rows = con.execute("""
            SELECT homeUser, awayUser,
                   CAST(homeScore AS INTEGER) AS hs,
                   CAST(awayScore AS INTEGER) AS aws,
                   CAST(seasonIndex AS INTEGER) AS season,
                   CAST(weekIndex AS INTEGER) AS week
            FROM games
            WHERE CAST(status AS TEXT) IN ('2', '3')
              AND homeUser IS NOT NULL AND homeUser != '' AND homeUser != 'CPU'
              AND awayUser IS NOT NULL AND awayUser != '' AND awayUser != 'CPU'
              AND homeScore IS NOT NULL AND CAST(homeScore AS INTEGER) >= 0
            ORDER BY CAST(seasonIndex AS INTEGER), CAST(weekIndex AS INTEGER)
        """).fetchall()
        con.close()

        total_pts = 0
        total_games = 0
        prev_season = None

        for home_user, away_user, hs, aws, season, week in rows:
            # Season regression at boundary
            if prev_season is not None and season != prev_season:
                for user in elo:
                    elo[user] = ELO_INITIAL + ELO_SEASON_REGRESS * (elo[user] - ELO_INITIAL)
            prev_season = season

            # Initialize new owners
            for user in (home_user, away_user):
                if user not in elo:
                    elo[user] = ELO_INITIAL
                    games_played[user] = 0

            # Get current Elos
            h_elo = elo[home_user]
            a_elo = elo[away_user]

            # Expected scores (standard Elo formula)
            exp_h = 1.0 / (1.0 + 10.0 ** ((a_elo - h_elo) / 400.0))
            exp_a = 1.0 - exp_h

            # Actual outcome
            if hs > aws:
                act_h, act_a = 1.0, 0.0
            elif aws > hs:
                act_h, act_a = 0.0, 1.0
            else:
                act_h, act_a = 0.5, 0.5

            # Margin of Victory multiplier (dampens blowouts)
            margin = abs(hs - aws)
            mov_mult = math.log(margin + 1) * 0.8 if margin > 0 else 0.5

            # K-factor based on games played
            for user, actual, expected in [
                (home_user, act_h, exp_h),
                (away_user, act_a, exp_a),
            ]:
                gp = games_played[user]
                if gp < 20:
                    k = ELO_K_NEW
                elif gp < 50:
                    k = ELO_K_MID
                else:
                    k = ELO_K_EST
                elo[user] += k * mov_mult * (actual - expected)
                games_played[user] += 1

            # Track scoring stats for O/U
            for user, scored, allowed in [
                (home_user, hs, aws),
                (away_user, aws, hs),
            ]:
                d = scoring.setdefault(user, {"pts_scored": 0, "pts_allowed": 0, "games": 0})
                d["pts_scored"]  += scored
                d["pts_allowed"] += allowed
                d["games"]       += 1

            total_pts   += hs + aws
            total_games += 1

        # Calibrate league average from actual data
        if total_games >= 10:
            _LEAGUE_AVG_SCORE = round((total_pts / total_games) / 2, 2)
            log.debug(f"[ELO] League avg pts/team: {_LEAGUE_AVG_SCORE:.1f} ({total_games} games)")
        else:
            _LEAGUE_AVG_SCORE = 30.0
            log.debug(f"[ELO] Not enough history — using fallback {_LEAGUE_AVG_SCORE}")

    except Exception as e:
        log.warning(f"[ELO] History query failed: {e}")
        _LEAGUE_AVG_SCORE = 30.0

    # Compute per-owner derived scoring stats
    for user, d in scoring.items():
        g = max(d["games"], 1)
        d["avg_pts_scored"]  = round(d["pts_scored"]  / g, 2)
        d["avg_pts_allowed"] = round(d["pts_allowed"] / g, 2)

    _ELO_CACHE = elo
    _OWNER_SCORING_CACHE = scoring

    # Log top/bottom Elo ratings
    sorted_elo = sorted(elo.items(), key=lambda x: x[1], reverse=True)
    log.debug(f"[ELO] Ratings computed for {len(elo)} owners")
    for user, rating in sorted_elo[:5]:
        gp = games_played.get(user, 0)
        log.debug(f"  TOP  {user}: {rating:.0f} ({gp} games)")
    for user, rating in sorted_elo[-3:]:
        gp = games_played.get(user, 0)
        log.debug(f"  BOT  {user}: {rating:.0f} ({gp} games)")

    return elo


def _safe_float(val, default: float) -> float:
    """Convert value to float, returning default only if val is None or empty string."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default: int) -> int:
    """Convert value to int, returning default only if val is None or empty string."""
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _resolve_elo(username: str, elo_map: dict) -> float:
    """Fuzzy username lookup for Elo — handles underscore/case differences."""
    if not username:
        return ELO_INITIAL
    if username in elo_map:
        return elo_map[username]
    norm = username.lower().replace("_", "").replace(" ", "")
    for key, val in elo_map.items():
        if key.lower().replace("_", "").replace(" ", "") == norm:
            return val
    return ELO_INITIAL


def _resolve_scoring(username: str) -> dict:
    """Get owner scoring stats, with fuzzy matching."""
    defaults = {"avg_pts_scored": _LEAGUE_AVG_SCORE, "avg_pts_allowed": _LEAGUE_AVG_SCORE, "games": 0}
    if not username:
        return defaults
    cache = _OWNER_SCORING_CACHE
    if username in cache:
        return cache[username]
    norm = username.lower().replace("_", "").replace(" ", "")
    for key, val in cache.items():
        if key.lower().replace("_", "").replace(" ", "") == norm:
            return val
    return defaults


def _get_power_map() -> dict:
    """Build { teamName: { ovr, win_pct, rank, off_rank, def_rank, userName } } from df_power.

    Uses _safe_float/_safe_int to avoid the Python `or` falsiness bug where
    legitimate 0 values (e.g. 0.000 win%) were silently replaced with defaults.
    """
    pm = {}
    if dm.df_power.empty:
        return pm
    for _, row in dm.df_power.iterrows():
        name = row.get("teamName", "")
        if not name:
            continue
        pm[name] = {
            "ovr":      _safe_float(row.get("ovrRating"),     78.0),
            "win_pct":  _safe_float(row.get("winPct"),         0.5),
            "rank":     _safe_int(row.get("rank"),              16),
            "off_rank": _safe_int(row.get("offTotalRank"),      16),
            "def_rank": _safe_int(row.get("defTotalRank"),      16),
            "userName": str(row.get("userName", "") or ""),
        }
    return pm


# ─────────────────────────────────────────────────────────────────────────────
# TEAM QUALITY SCORE
# ─────────────────────────────────────────────────────────────────────────────

def _team_quality_score(team_data: dict) -> float:
    """
    Compute a 0-100 team quality score from current API data.

    Components (weighted):
      50% — OVR rating   (normalized: 65-95 → 0-100)
      30% — Offense rank  (1=best → 100, 32=worst → 0)
      20% — Defense rank  (1=best → 100, 32=worst → 0)
    """
    ovr = team_data.get("ovr", 78.0)
    ovr_norm = max(0.0, min(100.0, (ovr - 65.0) / 30.0 * 100.0))

    off_rank = team_data.get("off_rank", 16)
    off_norm = max(0.0, min(100.0, (32 - off_rank) / 31.0 * 100.0))

    def_rank = team_data.get("def_rank", 16)
    def_norm = max(0.0, min(100.0, (32 - def_rank) / 31.0 * 100.0))

    return 0.50 * ovr_norm + 0.30 * off_norm + 0.20 * def_norm


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED POWER RATING
# ─────────────────────────────────────────────────────────────────────────────

def _combined_power(owner_elo: float, team_data: dict) -> float:
    """
    Blend owner Elo (80%) with team quality (20%) into a single power number.

    Owner Elo is normalized: 1200-1800 → 0-100.
    Team quality is already 0-100.
    Result range: 0-100.
    """
    elo_norm = max(0.0, min(100.0, (owner_elo - 1200.0) / 6.0))
    team_q   = _team_quality_score(team_data)
    return ELO_OWNER_WEIGHT * elo_norm + ELO_TEAM_WEIGHT * team_q


# ─────────────────────────────────────────────────────────────────────────────
# SPREAD, ML, O/U
# ─────────────────────────────────────────────────────────────────────────────

def _calc_spread(home_power: float, away_power: float) -> float:
    """
    Calculate point spread from HOME team's perspective.
    Negative = home favored. Positive = away favored.

    HOME_FIELD_EDGE is only added when home is already favored (per league rules:
    no phantom advantage in Madden where both players play remotely).

    Example: home_power = 65, away_power = 50
             raw = (50 - 65) / 4.0 = -3.75 → home is favored
             with HFE: -3.75 - 2.0 = -5.75 → rounds to home -6.0
    """
    raw = (away_power - home_power) / SPREAD_SCALING
    if raw < 0:  # home is already favored — add HFE to widen
        raw -= HOME_FIELD_EDGE
    spread = round(raw * 2) / 2
    return max(-SPREAD_CAP, min(SPREAD_CAP, spread))


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
    elif abs_s <= 14.0: base = 257 + int((abs_s - 10.0) * 12)
    else:               base = 305 + int((abs_s - 14.0) * 10)
    base = min(base, 800)
    return -base if spread < 0 else base


def _calc_ou(home_data: dict, away_data: dict,
             home_owner_scoring: dict, away_owner_scoring: dict) -> float:
    """
    Over/Under total points.

    Each team's expected score = owner's historical avg pts (or league avg if < 5 games)
      + offensive rank bonus/penalty  (0.35 per rank above/below midpoint)
      - opponent defensive rank penalty (0.22 per rank above/below midpoint)

    Total clamped to [35.0, 99.5] and rounded to nearest 0.5.
    Madden games average ~60 total pts but high-scoring matchups can hit 90+.
    """
    LAR = _MIDPOINT_RANK

    h_games = home_owner_scoring.get("games", 0)
    a_games = away_owner_scoring.get("games", 0)
    h_base = home_owner_scoring["avg_pts_scored"] if h_games >= 5 else _LEAGUE_AVG_SCORE
    a_base = away_owner_scoring["avg_pts_scored"] if a_games >= 5 else _LEAGUE_AVG_SCORE

    h_off_adj = (LAR - home_data["off_rank"]) * 0.35
    a_off_adj = (LAR - away_data["off_rank"]) * 0.35

    h_def_qual = (LAR - home_data["def_rank"]) * 0.22
    a_def_qual = (LAR - away_data["def_rank"]) * 0.22

    home_expected = max(7.0, h_base + h_off_adj - a_def_qual)
    away_expected = max(7.0, a_base + a_off_adj - h_def_qual)

    total = round((home_expected + away_expected) * 2) / 2
    return max(OU_FLOOR, min(OU_CEILING, total))


from odds_utils import american_to_str as _american_to_str, payout_calc as _payout_calc  # noqa: E402


def _combine_parlay_odds(odds_list: list[int]) -> int:
    """Combine multiple American odds into a single parlay American odds value."""
    decimal = 1.0
    for o in odds_list:
        o = int(o)
        if o == 0:
            continue  # skip legs with zero odds to avoid ZeroDivisionError
        decimal *= (1 + o / 100) if o > 0 else (1 + 100 / abs(o))
    if decimal <= 1.0:
        return 100  # fallback: even odds if all legs were zero/cancelled
    return int((decimal - 1) * 100) if decimal >= 2.0 else int(-100 / (decimal - 1))


def _build_game_lines(games_raw: list) -> list[dict]:
    """
    Build fully-calculated game lines using Elo-based power ratings.
    Admin line_overrides applied first; engine calculates any field not overridden.
    """
    power_map = _get_power_map()
    elo_map   = _compute_elo_ratings()

    _FALLBACK_TEAM = {
        "ovr": 78.0, "win_pct": 0.5, "rank": 16,
        "off_rank": 16, "def_rank": 16, "userName": ""
    }

    def fmt_spread(s: float) -> str:
        if s == 0: return "PK"
        return f"+{s}" if s > 0 else str(s)

    # Deduplicate by gameId — prefer records with gameTime populated
    seen = {}
    for rg in games_raw:
        gid = str(rg.get("gameId", rg.get("id", "")))
        if gid and gid in seen:
            if not seen[gid].get("gameTime") and rg.get("gameTime"):
                seen[gid] = rg
        else:
            seen[gid] = rg
    games_raw = list(seen.values())

    ui_games = []

    for rg in games_raw:
        home     = rg.get("homeTeamName", rg.get("home", ""))
        away     = rg.get("awayTeamName", rg.get("away", ""))
        game_id  = str(rg.get("gameId", rg.get("id", rg.get("matchup_key", f"{away}@{home}"))))
        status   = _safe_int(rg.get("status", 1), 1)
        week_idx = _safe_int(rg.get("weekIndex", 99), 99)

        # Auto-lock finished or past-week games
        if status >= 2 or week_idx < dm.CURRENT_WEEK:
            _set_locked(game_id, True)

        home_data = power_map.get(home, _FALLBACK_TEAM)
        away_data = power_map.get(away, _FALLBACK_TEAM)

        # Resolve Elo ratings and scoring stats for each owner
        home_user = home_data["userName"]
        away_user = away_data["userName"]
        home_elo  = _resolve_elo(home_user, elo_map)
        away_elo  = _resolve_elo(away_user, elo_map)
        home_scoring = _resolve_scoring(home_user)
        away_scoring = _resolve_scoring(away_user)

        # Combined power: 80% Elo + 20% team quality
        home_power = _combined_power(home_elo, home_data)
        away_power = _combined_power(away_elo, away_data)

        # ── Compute engine values ────────────────────────────────────────
        engine_home_spread = _calc_spread(home_power, away_power)
        engine_away_spread = -engine_home_spread
        engine_home_ml     = _spread_to_ml(engine_home_spread)
        engine_away_ml     = _spread_to_ml(engine_away_spread)
        engine_ou          = _calc_ou(home_data, away_data, home_scoring, away_scoring)

        # ── Apply admin overrides ────────────────────────────────────────
        ov = _get_line_override(game_id) or {}

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

        log.debug(
            f"[ELO] {away}({away_user} Elo={away_elo:.0f} P={away_power:.1f}) "
            f"@ {home}({home_user} Elo={home_elo:.0f} P={home_power:.1f}) "
            f"→ spread {fmt_spread(home_spread)}  O/U {ou_line}"
            + (" [OVERRIDE]" if ov else "")
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
            # Engine debug values
            "_engine_spread":  engine_home_spread,
            "_away_power":     away_power,
            "_home_power":     home_power,
            "_away_elo":       away_elo,
            "_home_elo":       home_elo,
            "_overridden":     bool(ov),
        })

    return ui_games


async def _write_tsl_events(games: list[dict]) -> None:
    """
    Write current-week TSL games into sportsbook_core events table.
    Called after _build_game_lines() so event rows exist before any bet is placed.
    """
    import sportsbook_core
    import data_manager as dm
    from datetime import datetime, timezone

    for g in games:
        game_id = str(g.get("game_id", ""))
        if game_id and game_id != "0":
            event_id = f"tsl:{game_id}"
        else:
            home_id = g.get("home", "")
            away_id = g.get("away", "")
            event_id = f"tsl:s{dm.CURRENT_SEASON}:w{g.get('bet_week', dm.CURRENT_WEEK)}:{home_id}v{away_id}"

        commence_ts = g.get("gameTime") or \
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        await sportsbook_core.write_event(
            event_id=event_id,
            source="TSL",
            home=g.get("home", ""),
            away=g.get("away", ""),
            commence_ts=commence_ts,
        )
        g["event_id"] = event_id  # store back for bet placement


# ═════════════════════════════════════════════════════════════════════════════
#  PARLAY CART (DB-backed, survives restarts)
# ═════════════════════════════════════════════════════════════════════════════

def _get_cart(uid: int) -> list[dict]:
    """Return all cart legs for a user as a list of dicts."""
    with _db_con() as con:
        rows = con.execute(
            "SELECT source, event_id, display, pick, bet_type, line, odds "
            "FROM parlay_cart WHERE discord_id = ? ORDER BY cart_leg_id",
            (uid,),
        ).fetchall()
    return [
        {"source": r[0], "event_id": r[1], "display": r[2], "pick": r[3],
         "bet_type": r[4], "line": r[5], "odds": r[6]}
        for r in rows
    ]


def _clear_cart(uid: int):
    """Remove all cart legs for a user."""
    with _db_con() as con:
        con.execute("DELETE FROM parlay_cart WHERE discord_id = ?", (uid,))


def _add_to_cart(uid: int, leg: dict) -> int:
    """Add a leg to the user's cart. Returns leg count, -1 if duplicate, -2 if full.

    Accepts normalized format (source/event_id/display) or legacy TSL format
    (game_id/matchup) and auto-normalizes.
    """
    if "source" not in leg:
        leg = {
            "source": "TSL",
            "event_id": leg["game_id"],
            "display": leg.get("matchup", ""),
            "pick": leg["pick"],
            "bet_type": leg["bet_type"],
            "line": leg.get("line", 0),
            "odds": leg["odds"],
        }

    with _db_con() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM parlay_cart WHERE discord_id = ?", (uid,)
        ).fetchone()[0]
        if count >= MAX_PARLAY_LEGS:
            return -2

        try:
            con.execute(
                "INSERT INTO parlay_cart "
                "(discord_id, source, event_id, display, pick, bet_type, line, odds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, leg["source"], leg["event_id"], leg["display"],
                 leg["pick"], leg["bet_type"], leg.get("line", 0), leg["odds"]),
            )
        except sqlite3.IntegrityError:
            return -1

        return count + 1


def _expire_stale_cart_legs():
    """Remove cart legs older than 24 hours."""
    with _db_con() as con:
        con.execute(
            "DELETE FROM parlay_cart WHERE added_at < datetime('now', '-24 hours')"
        )


# ═════════════════════════════════════════════════════════════════════════════
#  GRADING — settlement handled by sportsbook_core.settle_event via EVENT_FINALIZED
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
#  UI COMPONENTS
# ═════════════════════════════════════════════════════════════════════════════

class CustomWagerModal(discord.ui.Modal):
    """Text-input modal for custom (non-preset) wager amounts."""
    def __init__(self, team, line, odds, game_id, bet_type,
                 matchup_key, away_name, home_name, bet_week=None):
        super().__init__(title=f"📋 Custom Wager — {bet_type}")
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
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Enter a valid number.", ephemeral=True)
        await _place_straight_bet(
            interaction,
            team=self.team, line=self.line, odds=self.odds,
            game_id=self.game_id, bet_type=self.bet_type,
            matchup_key=self.matchup_key, away_name=self.away_name,
            home_name=self.home_name, bet_week=self.bet_week,
            amount=amt, already_deferred=False,
        )


async def _place_straight_bet(
    interaction: discord.Interaction,
    *,
    team: str,
    line,
    odds: int,
    game_id: str,
    bet_type: str,
    matchup_key: str,
    away_name: str,
    home_name: str,
    bet_week: int,
    amount: int,
    already_deferred: bool = False,
) -> None:
    """Shared straight-bet placement logic used by preset buttons and CustomWagerModal."""

    async def _send_error(msg: str):
        if already_deferred:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    if _is_locked(game_id):
        return await _send_error("🔴 Game is **locked**.")

    if amount < MIN_BET:
        return await _send_error(f"❌ Minimum bet is **${MIN_BET}**.")

    # Per-user lock prevents double-spend across concurrent bets
    async with flow_wallet.get_user_lock(interaction.user.id):
        try:
            with _db_con() as con:
                con.execute("BEGIN IMMEDIATE")
                safe_line = line if isinstance(line, (int, float)) else 0.0
                new_bal = flow_wallet.update_balance_sync(
                    interaction.user.id, -amount, source="TSL_BET", con=con,
                    subsystem="TSL_BET", subsystem_id="pending",
                )
                con.commit()
        except flow_wallet.InsufficientFundsError:
            balance = _get_balance(interaction.user.id)
            return await _send_error(f"❌ Insufficient balance. You have **${balance:,}**.")

        # Write bet to sportsbook_core (flow.db) after funds confirmed
        import sportsbook_core as _sb_core
        try:
            bet_id = await _sb_core.write_bet(
                discord_id=int(interaction.user.id),
                event_id=f"tsl:{game_id}",
                bet_type=bet_type,
                pick=team,
                line=safe_line if isinstance(safe_line, (int, float)) else None,
                odds=int(odds),
                wager=int(amount),
            )
        except Exception as e:
            log.exception(f"[SB] write_bet failed for uid={interaction.user.id} event_id={game_id}: {e}")
            # Refund the debit since we can't record the bet
            try:
                with _db_con() as con:
                    _update_balance(interaction.user.id, amount, con,
                                    subsystem="TSL_BET", subsystem_id="pending",
                                    reference_key="TSL_BET_WRITE_FAILED_REFUND")
                    con.commit()
            except Exception:
                log.exception("[SB] CRITICAL: refund also failed after write_bet failure")
            return await _send_error(
                "⚠️ Bet placement failed — your funds have been returned. Please try again."
            )

        import wager_registry
        with _db_con() as con:
            wager_registry.register_wager_sync(
                "TSL_BET", str(bet_id), int(interaction.user.id), int(amount),
                label=f"{team} {bet_type} {_american_to_str(odds)}",
                odds=int(odds), con=con,
            )

    profit = _payout_calc(amount, odds) - amount

    if not already_deferred:
        await interaction.response.defer(ephemeral=True)

    from sportsbook_cards import build_bet_confirm_card
    theme_id = get_theme_for_render(interaction.user.id)
    safe_line = line if isinstance(line, (int, float)) else None
    png = await build_bet_confirm_card(
        pick=team, bet_type=bet_type, odds=odds,
        risk=amount, to_win=profit, balance=new_bal,
        matchup=matchup_key, week=bet_week, line=safe_line,
        theme_id=theme_id,
    )
    await send_card(interaction, png, filename="bet_confirm.png", followup=True, ephemeral=True)

    # Post to #ledger
    try:
        txn_id = await flow_wallet.get_last_txn_id(interaction.user.id)
        from ledger_poster import post_transaction
        await post_transaction(
            interaction.client, interaction.guild_id, interaction.user.id,
            "TSL_BET", -amount, new_bal,
            f"Bet: {team} {bet_type} @ {_american_to_str(odds)}",
            txn_id,
        )
    except Exception:
        log.exception("Ledger post failed for straight bet")


class WagerPresetView(discord.ui.View):
    """DraftKings-style preset wager buttons. Works for any sport source.

    Args:
        pick: Display name of the selection (e.g., "Cowboys", "Chiefs")
        bet_type: "Spread", "Moneyline", "Over", "Under"
        odds: American odds (e.g., -110)
        display_info: Dict with keys: matchup, line_str, source_label
        user_balance: User's current TSL Bucks balance
        place_bet: Async callable (interaction, amount) -> None.
        parlay_leg: Normalized leg dict for _add_to_cart.
        custom_modal_factory: Callable () -> discord.ui.Modal for Custom button.
    """

    def __init__(self, *, pick: str, bet_type: str, odds: int, display_info: dict,
                 user_balance: int, place_bet, parlay_leg: dict, custom_modal_factory):
        super().__init__(timeout=120)
        self.pick = pick
        self.bet_type = bet_type
        self.odds = odds
        self.display_info = display_info
        self.user_balance = user_balance
        self._place_bet = place_bet
        self._parlay_leg = parlay_leg
        self._custom_modal_factory = custom_modal_factory

        # Row 0: preset amount buttons
        for amt in WAGER_PRESETS:
            can_afford = amt <= user_balance
            btn = discord.ui.Button(
                label=f"${amt:,}",
                style=discord.ButtonStyle.success if can_afford else discord.ButtonStyle.secondary,
                disabled=not can_afford,
                row=0,
            )
            btn.callback = self._make_preset_cb(amt)
            self.add_item(btn)

        # Row 1: Custom + Add to Parlay
        custom_btn = discord.ui.Button(
            label="✏️ Custom",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        custom_btn.callback = self._custom_cb
        self.add_item(custom_btn)

        parlay_btn = discord.ui.Button(
            label="🎰 Add to Parlay",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        parlay_btn.callback = self._parlay_cb
        self.add_item(parlay_btn)

    def _build_embed(self) -> discord.Embed:
        info = self.display_info
        embed = discord.Embed(
            title=f"📋  {self.pick} — {self.bet_type}", color=TSL_GOLD,
        )
        source_badge = (f"**[{info.get('source_label', 'TSL')}]** "
                        if info.get('source_label') not in (None, 'TSL') else "")
        embed.description = (
            f"{source_badge}**{info['matchup']}**\n"
            f"Line: `{info['line_str']}`\n\n"
            f"💰 Balance: **${self.user_balance:,}**"
        )
        return embed

    def _make_preset_cb(self, amt: int):
        async def callback(interaction: discord.Interaction):
            try:
                await self._place_bet(interaction, amt)
            except flow_wallet.InsufficientFundsError:
                balance = _get_balance(interaction.user.id)
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"❌ Insufficient balance. You have **${balance:,}**.", ephemeral=True)
                else:
                    await interaction.followup.send(
                        f"❌ Insufficient balance. You have **${balance:,}**.", ephemeral=True)
        return callback

    async def _custom_cb(self, interaction: discord.Interaction):
        modal = self._custom_modal_factory()
        await interaction.response.send_modal(modal)

    async def _parlay_cb(self, interaction: discord.Interaction):
        uid = interaction.user.id
        result = _add_to_cart(uid, self._parlay_leg)
        if result == -1:
            return await interaction.response.send_message(
                "⚠️ You already have a leg from this game in your parlay cart.", ephemeral=True
            )
        if result == -2:
            return await interaction.response.send_message(
                f"⚠️ Cart is full — max **{MAX_PARLAY_LEGS}** legs.", ephemeral=True
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

        if amt < MIN_BET:
            return await interaction.response.send_message(f"❌ Min bet is **${MIN_BET}**.", ephemeral=True)

        # Per-user lock prevents double-spend across concurrent bets
        async with flow_wallet.get_user_lock(interaction.user.id):
            # Atomic balance check + debit inside single transaction
            parlay_id = str(uuid.uuid4())[:8].upper()
            try:
                with _db_con() as con:
                    con.execute("BEGIN IMMEDIATE")
                    con.execute(
                        "INSERT INTO parlays_table "
                        "(parlay_id, discord_id, week, combined_odds, wager_amount, status) "
                        "VALUES (?, ?, ?, ?, ?, 'Pending')",
                        (parlay_id, int(interaction.user.id), int(dm.CURRENT_WEEK + 1),
                         int(self.combined_odds), int(amt))
                    )
                    # Insert normalized legs (primary data source)
                    for i, leg in enumerate(self.legs):
                        source = leg.get("source", "TSL")
                        event_id = leg.get("event_id", leg.get("game_id", ""))
                        display = leg.get("display", leg.get("matchup", ""))
                        con.execute(
                            "INSERT INTO parlay_legs "
                            "(parlay_id, leg_index, game_id, matchup, pick, bet_type, line, odds, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (parlay_id, i, event_id, display, leg["pick"],
                             leg["bet_type"], leg.get("line", 0), leg["odds"], source),
                        )
                    flow_wallet.update_balance_sync(
                        interaction.user.id, -amt, source="TSL_BET", con=con,
                        subsystem="PARLAY", subsystem_id=parlay_id,
                    )
                    import wager_registry
                    leg_summary = " / ".join(l["pick"] for l in self.legs)
                    wager_registry.register_wager_sync(
                        "PARLAY", parlay_id, int(interaction.user.id), int(amt),
                        label=f"Parlay ({len(self.legs)}L): {leg_summary[:80]}",
                        odds=int(self.combined_odds), con=con,
                    )
                    con.commit()
            except flow_wallet.InsufficientFundsError:
                balance = _get_balance(interaction.user.id)
                return await interaction.response.send_message(
                    f"❌ Insufficient funds. Balance: **${balance:,}**.\n"
                    f"Your parlay cart has been preserved — add funds and try again.",
                    ephemeral=True,
                )

        # Mirror parlay + legs into sportsbook_core (flow.db)
        _parlay_mirrored = False
        _mirrored_bet_ids: list[int] = []
        try:
            import sportsbook_core as _sb_core
            await _sb_core.write_parlay(
                parlay_id=parlay_id,
                discord_id=int(interaction.user.id),
                combined_odds=int(self.combined_odds),
                wager=int(amt),
            )
            _parlay_mirrored = True
            for i, leg in enumerate(self.legs):
                source = leg.get("source", "TSL")
                raw_game_id = leg.get("event_id", leg.get("game_id", ""))
                event_id = f"tsl:{raw_game_id}" if source == "TSL" else str(raw_game_id)
                leg_line = leg.get("line")
                leg_line_val = float(leg_line) if leg_line not in (None, "", 0) else None
                leg_bet_id = await _sb_core.write_bet(
                    discord_id=int(interaction.user.id),
                    event_id=event_id,
                    bet_type=leg["bet_type"],
                    pick=leg["pick"],
                    line=leg_line_val,
                    odds=int(leg["odds"]),
                    wager=int(amt),
                    parlay_id=parlay_id,
                )
                _mirrored_bet_ids.append(leg_bet_id)
                await _sb_core.write_parlay_leg(
                    parlay_id=parlay_id,
                    leg_index=i,
                    bet_id=leg_bet_id,
                )
        except Exception:
            log.error(
                "[PARLAY] mirror failed — parlay_id=%s discord_id=%s amt=%s legs=%s; attempting cleanup",
                parlay_id, interaction.user.id, amt, len(self.legs),
            )
            try:
                import aiosqlite as _aiosqlite
                async with _aiosqlite.connect(_sb_core.FLOW_DB) as _db:
                    for _bid in _mirrored_bet_ids:
                        await _db.execute("DELETE FROM bets WHERE bet_id=?", (_bid,))
                    if _parlay_mirrored:
                        await _db.execute("DELETE FROM parlays WHERE parlay_id=?", (parlay_id,))
                    await _db.commit()
            except Exception:
                log.exception("[PARLAY] cleanup also failed for parlay_id=%s", parlay_id)

        potential = _payout_calc(amt, self.combined_odds) - amt
        _clear_cart(interaction.user.id)

        await interaction.response.defer(ephemeral=True)
        from sportsbook_cards import build_parlay_confirm_card
        theme_id = get_theme_for_render(interaction.user.id)
        png = await build_parlay_confirm_card(
            legs=self.legs, combined_odds=self.combined_odds,
            risk=amt, to_win=potential, week=dm.CURRENT_WEEK + 1,
            theme_id=theme_id,
        )
        await send_card(interaction, png, filename="parlay_confirm.png", followup=True, ephemeral=True)

        # Post to #ledger
        try:
            new_bal = _get_balance(interaction.user.id)
            txn_id = await flow_wallet.get_last_txn_id(interaction.user.id)
            from ledger_poster import post_transaction
            leg_summary = " + ".join(l["pick"] for l in self.legs)
            await post_transaction(
                interaction.client, interaction.guild_id, interaction.user.id,
                "TSL_BET", -amt, new_bal,
                f"Parlay ({len(self.legs)}L): {leg_summary[:60]}",
                txn_id,
            )
        except Exception:
            log.exception("Ledger post failed for parlay bet")


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

        if amt < MIN_BET:
            return await interaction.response.send_message(f"❌ Min bet is **${MIN_BET}**.", ephemeral=True)

        # Per-user lock prevents double-spend across concurrent bets
        async with flow_wallet.get_user_lock(interaction.user.id):
            # Atomic: verify prop open + balance check + debit in single transaction
            try:
                with _db_con() as con:
                    con.execute("BEGIN IMMEDIATE")
                    prop = con.execute(
                        "SELECT status FROM prop_bets WHERE prop_id=?", (self.prop_id,)
                    ).fetchone()
                    if not prop or prop[0] != 'Open':
                        con.rollback()
                        return await interaction.response.send_message(
                            "❌ This prop bet is no longer open.", ephemeral=True
                        )
                    con.execute(
                        "INSERT INTO prop_wagers (prop_id, discord_id, pick, wager_amount, odds) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (self.prop_id, int(interaction.user.id), self.pick, int(amt), int(self.odds))
                    )
                    wager_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                    new_bal = flow_wallet.update_balance_sync(
                        interaction.user.id, -amt, source="TSL_BET", con=con,
                        subsystem="PROP", subsystem_id=str(wager_id),
                    )
                    import wager_registry
                    wager_registry.register_wager_sync(
                        "PROP", str(wager_id), int(interaction.user.id), int(amt),
                        label=f"Prop #{self.prop_id}: {self.pick}",
                        odds=int(self.odds), con=con,
                    )
                    con.commit()
            except flow_wallet.InsufficientFundsError:
                balance = _get_balance(interaction.user.id)
                return await interaction.response.send_message(
                    f"❌ Insufficient funds. Balance: **${balance:,}**.", ephemeral=True
                )

        profit = _payout_calc(amt, self.odds) - amt
        embed = discord.Embed(title="✅ Prop Bet Confirmed", color=TSL_GOLD)
        embed.add_field(name="Prop",    value=self.description[:50], inline=False)
        embed.add_field(name="Pick",    value=f"**{self.pick}**",    inline=True)
        embed.add_field(name="Odds",    value=_american_to_str(self.odds), inline=True)
        embed.add_field(name="Risk",    value=f"**${amt:,}**",       inline=True)
        embed.add_field(name="To Win",  value=f"**${profit:,}**",    inline=True)
        embed.add_field(name="Balance", value=f"${new_bal:,}",       inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Post to #ledger
        try:
            txn_id = await flow_wallet.get_last_txn_id(interaction.user.id)
            from ledger_poster import post_transaction
            await post_transaction(
                interaction.client, interaction.guild_id, interaction.user.id,
                "TSL_BET", -amt, new_bal,
                f"Prop: {self.pick} — {self.description[:50]}",
                txn_id,
            )
        except Exception:
            log.exception("Ledger post failed for prop bet")


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

    def _make_straight_cb(self, pick, line, odds, bet_type):
        game = self.game
        async def callback(interaction: discord.Interaction):
            if _is_locked(game["game_id"]):
                return await interaction.response.send_message("🔴 Game is **locked**.", ephemeral=True)
            balance = _get_balance(interaction.user.id)
            bet_week = game.get("bet_week", dm.CURRENT_WEEK + 1)

            async def place_bet(inter, amt):
                await inter.response.defer(ephemeral=True)
                await _place_straight_bet(
                    inter, team=pick, line=line, odds=odds,
                    game_id=game["game_id"], bet_type=bet_type,
                    matchup_key=game["matchup_key"],
                    away_name=game["away"], home_name=game["home"],
                    bet_week=bet_week, amount=amt, already_deferred=True,
                )

            view = WagerPresetView(
                pick=pick, bet_type=bet_type, odds=odds,
                display_info={
                    "matchup": f"{game['away']} @ {game['home']}",
                    "line_str": _american_to_str(odds),
                    "source_label": "TSL",
                },
                user_balance=balance,
                place_bet=place_bet,
                parlay_leg={
                    "source": "TSL",
                    "event_id": game["game_id"],
                    "display": f"{game['away']} @ {game['home']}",
                    "pick": pick,
                    "bet_type": bet_type,
                    "line": line,
                    "odds": odds,
                },
                custom_modal_factory=lambda: CustomWagerModal(
                    team=pick, line=line, odds=odds, game_id=game["game_id"],
                    bet_type=bet_type, matchup_key=game["matchup_key"],
                    away_name=game["away"], home_name=game["home"],
                    bet_week=bet_week,
                ),
            )
            embed = view._build_embed()
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


# ═════════════════════════════════════════════════════════════════════════════
#  HELPER — Load TSL week games for the hub
# ═════════════════════════════════════════════════════════════════════════════

async def _load_tsl_week_games() -> list[dict]:
    """Load current-week TSL game lines for the sportsbook UI."""
    bet_week = dm.CURRENT_WEEK + 1
    src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
    if src.empty:
        raise ValueError("No game data loaded. Try again shortly.")

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
        raise ValueError(f"No games found for Week {bet_week}. Schedule may not be posted yet.")

    loop = asyncio.get_running_loop()
    games = await loop.run_in_executor(None, _build_game_lines, raw_games)
    await _write_tsl_events(games)
    return games


# ═════════════════════════════════════════════════════════════════════════════
#  SPORTSBOOK WORKSPACE — Edit-in-place single-message drill-down
# ═════════════════════════════════════════════════════════════════════════════

# Map short sport IDs → odds_api_client sport keys
_SPORT_KEY_MAP: dict[str, str | None] = {
    "tsl":   None,  # TSL uses internal data, not odds API
    "nfl":   "americanfootball_nfl",
    "nba":   "basketball_nba",
    "mlb":   "baseball_mlb",
    "nhl":   "icehockey_nhl",
    "ncaab": "basketball_ncaab",
    "ufc":   "mma_ufc",
    "epl":   "soccer_epl",
    "mls":   "soccer_mls",
    "wnba":  "basketball_wnba",
}

# Sport buttons layout: (label, emoji, sport_id, row)
_SPORT_BUTTONS = [
    ("TSL",   "\U0001f3c8", "tsl",   0),
    ("NFL",   "\U0001f3c8", "nfl",   0),
    ("NBA",   "\U0001f3c0", "nba",   0),
    ("MLB",   "\u26be",     "mlb",   0),
    ("NHL",   "\U0001f3d2", "nhl",   0),
    ("NCAAB", "\U0001f3c0", "ncaab", 1),
    ("UFC",   "\U0001f94a", "ufc",   1),
    ("EPL",   "\u26bd",     "epl",   1),
    ("MLS",   "\u26bd",     "mls",   1),
    ("WNBA",  "\U0001f3c0", "wnba",  1),
]


class SportsbookWorkspace(discord.ui.View):
    """Edit-in-place workspace for sportsbook drill-downs.

    All child states render into a single ephemeral message.
    Navigation never spawns new messages — it calls _refresh().
    """

    def __init__(self, cog: "SportsbookCog", user_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user_id = user_id
        # State caches (populated by show_* methods)
        self._cached_tsl_games: list[dict] = []
        self._cached_real_events: list[dict] = []
        self._current_game: dict = {}
        self._current_event: dict = {}
        self._current_sport_key: str = ""
        self._pending_bet: dict = {}
        self._parlay_mode: bool = False

    # ── Core mechanics ──────────────────────────────────────────────────────

    def _cart_footer(self) -> str:
        """Build a one-line cart summary for embed footers."""
        cart = _get_cart(self.user_id)
        if not cart:
            return "Cart empty \u2022 Add legs from any sport"
        legs_str = " + ".join(
            f"{l['pick']} ({l['bet_type']}) {_american_to_str(l['odds'])}"
            for l in cart
        )
        if len(cart) >= 2:
            combined = _combine_parlay_odds([l["odds"] for l in cart])
            return f"\U0001f3b0 Cart [{len(cart)}]: {legs_str} | Combined: {_american_to_str(combined)} | Ready to submit"
        return f"\U0001f3b0 Cart [{len(cart)}]: {legs_str} | Add more legs"

    async def _update_workspace(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        view: "SportsbookWorkspace",
        *,
        file: discord.File | None = None,
    ):
        """Edit the workspace message in-place."""
        embed.set_footer(text=self._cart_footer())

        kwargs: dict = {"embed": embed, "view": view}
        if file is not None:
            embed.set_image(url=f"attachment://{file.filename}")
            kwargs["attachments"] = [file]
        else:
            embed.set_image(url=None)
            kwargs["attachments"] = []

        if not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.edit_original_response(**kwargs)

    def _add_cart_buttons(self, row: int):
        """Add Submit Parlay + Clear Cart buttons if cart has legs."""
        cart = _get_cart(self.user_id)
        if not cart:
            return
        combined = _combine_parlay_odds([l["odds"] for l in cart])
        submit = discord.ui.Button(
            label=f"\U0001f4b0 Submit ({len(cart)}L @ {_american_to_str(combined)})",
            style=discord.ButtonStyle.success,
            disabled=len(cart) < 2,
            row=row,
        )
        submit.callback = self._submit_parlay_cb
        self.add_item(submit)

        clear = discord.ui.Button(label="\U0001f5d1\ufe0f Clear", style=discord.ButtonStyle.danger, row=row)
        clear.callback = self._clear_cart_cb
        self.add_item(clear)

    # ── State 1: Sport Selector ─────────────────────────────────────────────

    async def show_sport_selector(self, interaction: discord.Interaction, *, is_initial: bool = False):
        """Sport selector — the 'home' state of the workspace."""
        self.clear_items()

        balance = _get_balance(self.user_id)

        if self._parlay_mode:
            embed = discord.Embed(title="\U0001f3b0  BUILD A PARLAY", color=TSL_GOLD)
            embed.description = (
                f"\U0001f4b0 **Balance:** ${balance:,}\n"
                f"*Parlay mode \u2014 selecting a bet will add it as a leg.*\n"
                f"Select a sport to browse games."
            )
        else:
            embed = discord.Embed(title="\U0001f3c6  ATLAS SPORTSBOOK", color=TSL_GOLD)
            embed.description = (
                f"\U0001f4b0 **Balance:** ${balance:,}\n"
                f"Select a sport to browse games."
            )

        for label, emoji, sport_id, row in _SPORT_BUTTONS:
            btn = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=row)
            btn.callback = self._make_sport_cb(sport_id)
            self.add_item(btn)

        # Row 2: Cart controls
        self._add_cart_buttons(row=2)

        if is_initial:
            embed.set_footer(text=self._cart_footer())
            await interaction.followup.send(embed=embed, view=self, ephemeral=True)
        else:
            await self._update_workspace(interaction, embed, self)

    # ── State 2: TSL Game List ──────────────────────────────────────────────

    async def show_tsl_games(self, interaction: discord.Interaction):
        """TSL game list with select dropdown."""
        self.clear_items()

        ui_games = self._cached_tsl_games
        bet_week = dm.CURRENT_WEEK + 1
        balance = _get_balance(self.user_id)

        embed = discord.Embed(title="\U0001f3c8  TSL SPORTSBOOK", color=TSL_GOLD)
        embed.description = (
            f"```\n"
            f"WEEK {bet_week} BOARD  \u2022  SEASON {dm.CURRENT_SEASON}\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4b0 Your Balance:  ${balance:,}\n"
            f"\U0001f3ae Games:         {len(ui_games)}\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"SELECT A GAME TO PLACE WAGER\n"
            f"```"
        )

        options = []
        for g in ui_games:
            locked = "\U0001f534 " if _is_locked(g["game_id"]) else "\U0001f7e2 "
            over = " \u26a0\ufe0f" if g.get("_overridden") else ""
            options.append(discord.SelectOption(
                label=f"{locked}{g['away']} @ {g['home']}{over}",
                value=str(g["game_id"]),
                description=f"Spread: {g['away']} {g['away_spread']} | O/U {g['ou_line']}"
            ))

        sel = discord.ui.Select(
            placeholder="\u2501\u2501 SELECT A GAME \u2501\u2501",
            options=options[:25],
            min_values=1, max_values=1, row=0,
        )
        sel.callback = self._on_tsl_game_select
        self.add_item(sel)

        back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back_to_sports
        self.add_item(back)
        self._add_cart_buttons(row=1)

        await self._update_workspace(interaction, embed, self)

    # ── State 3: TSL Match Detail ───────────────────────────────────────────

    async def show_tsl_match(self, interaction: discord.Interaction, game: dict):
        """TSL match detail with bet type buttons + card image."""
        self.clear_items()
        self._current_game = game

        is_locked = _is_locked(game["game_id"])
        theme_id = get_theme_for_render(self.user_id)
        png = await build_match_detail_card(game, locked=is_locked, theme_id=theme_id)
        file = card_to_file(png, f"match_{game['game_id']}.png")

        embed = discord.Embed(color=TSL_GOLD)

        away, home = game["away"], game["home"]
        ou = game["ou_line"]

        bets = [
            (f"{away} {game['away_spread']} (\u2212110)", away, game["away_spread_val"], -110, "Spread",    0),
            (f"{home} {game['home_spread']} (\u2212110)", home, game["home_spread_val"], -110, "Spread",    0),
            (f"{away} ML {game['away_ml']}",              away, 0.0,                     game["away_ml_val"], "Moneyline", 1),
            (f"{home} ML {game['home_ml']}",              home, 0.0,                     game["home_ml_val"], "Moneyline", 1),
            (f"OVER {ou} (\u2212110)",                    f"OVER {ou}", ou,               -110, "Over",      2),
            (f"UNDER {ou} (\u2212110)",                   f"UNDER {ou}", ou,              -110, "Under",     2),
        ]

        for label, pick, line, odds, bet_type, row in bets:
            btn = discord.ui.Button(
                label=label[:80],
                style=(discord.ButtonStyle.secondary if row == 0 else
                       discord.ButtonStyle.primary   if row == 1 else
                       discord.ButtonStyle.success   if "OVER" in label else
                       discord.ButtonStyle.danger),
                row=row,
                disabled=is_locked,
            )
            btn.callback = self._make_bet_cb(game, pick, line, odds, bet_type)
            self.add_item(btn)

        back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
        back.callback = self._back_to_tsl_games
        self.add_item(back)

        await self._update_workspace(interaction, embed, self, file=file)

    # ── State 4: Real Sport Game List ───────────────────────────────────────

    async def show_real_games(self, interaction: discord.Interaction, sport_key: str):
        """Real sports game list — select dropdown."""
        self.clear_items()
        self._current_sport_key = sport_key

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT event_id, sport_key, home_team, away_team, commence_time "
                "FROM real_events WHERE sport_key = ? AND commence_time > ? "
                "ORDER BY commence_time",
                (sport_key, now_str),
            ) as cur:
                events = [dict(row) for row in await cur.fetchall()]

        self._cached_real_events = events

        if not events:
            sport_name = REAL_SPORTS.get(sport_key, sport_key)
            embed = discord.Embed(
                description=f"No **{sport_name}** games available right now.\nOdds sync on a schedule \u2014 check back later!",
                color=TSL_GOLD,
            )
            back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
            back.callback = self._back_to_sports
            self.add_item(back)
            await self._update_workspace(interaction, embed, self)
            return

        sport_name = REAL_SPORTS.get(sport_key, sport_key)
        emoji = SPORT_EMOJI.get(sport_key, "\U0001f3c6")

        embed = discord.Embed(
            title=f"{emoji} {sport_name} \u2014 Upcoming Games",
            description=f"**{len(events)}** games available for betting.",
            color=TSL_GOLD,
        )

        lines = []
        for ev in events[:10]:
            ct = _parse_commence(ev["commence_time"])
            ts = f"<t:{int(ct.timestamp())}:R>" if ct else "TBD"
            lines.append(f"**{ev['away_team']}** @ **{ev['home_team']}** \u2014 {ts}")
        embed.add_field(name="Games", value="\n".join(lines) or "No games scheduled right now.", inline=False)

        options = []
        for ev in events[:25]:
            ct = _parse_commence(ev["commence_time"])
            time_str = ct.strftime("%m/%d %I:%M %p") if ct else "TBD"
            label = f"{ev['away_team']} @ {ev['home_team']}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(label=label, value=ev["event_id"], description=time_str))

        sel = discord.ui.Select(placeholder="Select a game to bet on...", options=options, row=1)
        sel.callback = self._on_real_game_select
        self.add_item(sel)

        back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._back_to_sports
        self.add_item(back)
        self._add_cart_buttons(row=2)

        await self._update_workspace(interaction, embed, self)

    # ── State 5: Real Sport Match Detail (6-button grid) ────────────────────

    async def show_real_match(self, interaction: discord.Interaction, event: dict):
        """Real sport match detail — card image + 6-button grid (flat odds from real_events)."""
        self.clear_items()
        self._current_event = event
        sport_key = self._current_sport_key

        # Fetch full event row with flat odds columns
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM real_events WHERE event_id = ?",
                (event["event_id"],),
            ) as cur:
                row = await cur.fetchone()

        if not row:
            embed = discord.Embed(description="No odds available for this game yet.", color=TSL_GOLD)
            back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
            back.callback = lambda i: self._nav_real_games(i)
            self.add_item(back)
            await self._update_workspace(interaction, embed, self)
            return

        event_row = dict(row)
        has_odds = any(event_row.get(k) is not None for k in
                       ("moneyline_home", "spread_home", "over_under"))
        if not has_odds:
            embed = discord.Embed(description="No odds available for this game yet.", color=TSL_GOLD)
            back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
            back.callback = lambda i: self._nav_real_games(i)
            self.add_item(back)
            await self._update_workspace(interaction, embed, self)
            return

        theme_id = get_theme_for_render(self.user_id)
        png = await build_real_match_detail_card(event_row, sport_key=sport_key, theme_id=theme_id)
        file = card_to_file(png, f"match_{event['event_id']}.png")

        embed = discord.Embed(color=TSL_GOLD)

        home = event_row["home_team"]
        away = event_row["away_team"]
        short_home = _real_short_name(home)
        short_away = _real_short_name(away)

        # Build 6-button grid from flat odds columns
        for side, team_full, short, row_idx in [
            ("home", home, short_home, 0),
            ("away", away, short_away, 1),
        ]:
            # Moneyline
            ml_val = event_row.get(f"moneyline_{side}")
            if ml_val is not None:
                label = f"{short} {int(ml_val):+d}"
                btn = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.green, row=row_idx)
                btn.callback = self._make_real_bet_cb(event_row, team_full, "Moneyline", int(ml_val), None)
                self.add_item(btn)

            # Spread
            sp_val = event_row.get(f"spread_{side}")
            sp_odds = event_row.get(f"spread_{side}_odds")
            if sp_val is not None and sp_odds is not None:
                point_str = f"{float(sp_val):+g}"
                label = f"{short} {point_str} ({int(sp_odds):+d})"
                btn = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.blurple, row=row_idx)
                btn.callback = self._make_real_bet_cb(event_row, team_full, "Spread", int(sp_odds), float(sp_val))
                self.add_item(btn)

            # Totals (Over for home row, Under for away row)
            ou_name = "Over" if side == "home" else "Under"
            ou_total = event_row.get("over_under")
            ou_odds_key = "over_odds" if side == "home" else "under_odds"
            ou_odds = event_row.get(ou_odds_key)
            if ou_total is not None and ou_odds is not None:
                label = f"{ou_name} {float(ou_total)} ({int(ou_odds):+d})"
                btn = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.gray, row=row_idx)
                btn.callback = self._make_real_bet_cb(event_row, ou_name, ou_name, int(ou_odds), float(ou_total))
                self.add_item(btn)

        back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=2)
        back.callback = lambda i: self._nav_real_games(i)
        self.add_item(back)
        self._add_cart_buttons(row=2)

        await self._update_workspace(interaction, embed, self, file=file)

    # ── State 6: Wager Presets ──────────────────────────────────────────────

    async def show_wager(self, interaction: discord.Interaction, game: dict,
                         pick: str, line, odds: int, bet_type: str):
        """Wager amount selection — preset buttons + custom + parlay."""
        self.clear_items()

        self._pending_bet = {
            "game": game, "pick": pick, "line": line, "odds": odds, "bet_type": bet_type,
        }

        balance = _get_balance(self.user_id)
        source = game.get("_source_label", "TSL")
        matchup = f"{game.get('away', game.get('away_team', ''))} @ {game.get('home', game.get('home_team', ''))}"

        embed = discord.Embed(
            title=f"\U0001f4cb  {pick} \u2014 {bet_type}", color=TSL_GOLD,
        )
        source_badge = f"**[{source}]** " if source not in (None, "TSL") else ""
        embed.description = (
            f"{source_badge}**{matchup}**\n"
            f"Line: `{_american_to_str(odds)}`\n\n"
            f"\U0001f4b0 Balance: **${balance:,}**\n\n"
            f"*Pick an amount to place a straight bet, or add this pick to your parlay cart.*"
        )

        # Row 0: Preset amount buttons
        for amt in WAGER_PRESETS:
            can_afford = amt <= balance
            btn = discord.ui.Button(
                label=f"${amt:,}",
                style=discord.ButtonStyle.success if can_afford else discord.ButtonStyle.secondary,
                disabled=not can_afford,
                row=0,
            )
            btn.callback = self._make_place_bet_cb(amt)
            self.add_item(btn)

        # Row 1: Custom amount (straight bet)
        custom_btn = discord.ui.Button(label="\u270f\ufe0f Custom Amount", style=discord.ButtonStyle.secondary, row=1)
        custom_btn.callback = self._custom_wager_cb
        self.add_item(custom_btn)

        # Row 2: Add to Parlay (distinct flow — own row)
        parlay_btn = discord.ui.Button(label="\U0001f3b0 Add to Parlay", style=discord.ButtonStyle.success, row=2)
        parlay_btn.callback = self._add_to_parlay_cb
        self.add_item(parlay_btn)

        # Row 3: Back
        back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=3)
        back.callback = self._back_to_match
        self.add_item(back)

        await self._update_workspace(interaction, embed, self)

    # ── State 7: Parlay Review ──────────────────────────────────────────────

    async def show_parlay_review(self, interaction: discord.Interaction):
        """Show cart legs before opening wager modal — single submission path."""
        self.clear_items()
        cart = _get_cart(self.user_id)
        if len(cart) < 2:
            embed = discord.Embed(
                description="\u274c A parlay requires at least **2 legs**.", color=TSL_GOLD
            )
            back = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=0)
            back.callback = self._back_to_sports
            self.add_item(back)
            await self._update_workspace(interaction, embed, self)
            return

        combined = _combine_parlay_odds([l["odds"] for l in cart])
        embed = discord.Embed(
            title="\U0001f3b0  PARLAY REVIEW",
            color=TSL_GOLD,
            description=(
                f"**{len(cart)} Legs** \u2014 Combined: **{combined:+d}**\n"
                f"*Review your legs, then enter your wager.*"
            ),
        )
        for i, leg in enumerate(cart, 1):
            source_badge = f"[{leg.get('source', 'TSL')}] " if leg.get('source') != 'TSL' else ""
            embed.add_field(
                name=f"Leg {i}: {source_badge}{leg['pick']}",
                value=f"{leg['bet_type']} ({leg['odds']:+d})\n{leg.get('display', '')}",
                inline=False,
            )

        submit_btn = discord.ui.Button(
            label=f"\U0001f4b0 Enter Wager ({_american_to_str(combined)})",
            style=discord.ButtonStyle.success,
            row=0,
        )
        clear_btn = discord.ui.Button(label="\U0001f5d1\ufe0f Clear", style=discord.ButtonStyle.danger, row=0)
        back_btn = discord.ui.Button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)

        async def _open_modal(i: discord.Interaction):
            await i.response.send_modal(ParlayWagerModal(self.user_id, cart, combined))

        submit_btn.callback = _open_modal
        clear_btn.callback = self._clear_cart_cb
        back_btn.callback = self._back_to_sports
        self.add_item(submit_btn)
        self.add_item(clear_btn)
        self.add_item(back_btn)

        await self._update_workspace(interaction, embed, self)

    # ── Classmethod: Open workspace from hub ────────────────────────────────

    @classmethod
    async def open_to_sport(cls, interaction: discord.Interaction, cog, sport_id: str):
        """Create workspace and open directly to a sport's game list.

        Called from SportsbookHubView button callbacks.
        interaction must already be deferred.
        """
        ws = cls(cog, interaction.user.id)

        if sport_id == "tsl":
            ws._cached_tsl_games = await _load_tsl_week_games()
            # Build the game list — we need to send the initial message
            bet_week = dm.CURRENT_WEEK + 1
            balance = _get_balance(interaction.user.id)

            embed = discord.Embed(title="\U0001f3c8  TSL SPORTSBOOK", color=TSL_GOLD)
            embed.description = (
                f"```\n"
                f"WEEK {bet_week} BOARD  \u2022  SEASON {dm.CURRENT_SEASON}\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f4b0 Your Balance:  ${balance:,}\n"
                f"\U0001f3ae Games:         {len(ws._cached_tsl_games)}\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"SELECT A GAME TO PLACE WAGER\n"
                f"```"
            )

            options = []
            for g in ws._cached_tsl_games:
                locked = "\U0001f534 " if _is_locked(g["game_id"]) else "\U0001f7e2 "
                over = " \u26a0\ufe0f" if g.get("_overridden") else ""
                options.append(discord.SelectOption(
                    label=f"{locked}{g['away']} @ {g['home']}{over}",
                    value=str(g["game_id"]),
                    description=f"Spread: {g['away']} {g['away_spread']} | O/U {g['ou_line']}"
                ))

            sel = discord.ui.Select(
                placeholder="\u2501\u2501 SELECT A GAME \u2501\u2501",
                options=options[:25],
                min_values=1, max_values=1, row=0,
            )
            sel.callback = ws._on_tsl_game_select
            ws.add_item(sel)

            back = discord.ui.Button(label="\u2190 Sports", style=discord.ButtonStyle.secondary, row=1)
            back.callback = ws._back_to_sports
            ws.add_item(back)
            ws._add_cart_buttons(row=1)

            embed.set_footer(text=ws._cart_footer())
            await interaction.followup.send(embed=embed, view=ws, ephemeral=True)

        else:
            sport_key = _SPORT_KEY_MAP.get(sport_id)
            if not sport_key:
                return await interaction.followup.send(f"\u274c Unknown sport: `{sport_id}`", ephemeral=True)
            ws._current_sport_key = sport_key

            # Load events
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT event_id, sport_key, home_team, away_team, commence_time "
                    "FROM real_events WHERE sport_key = ? AND commence_time > ? "
                    "ORDER BY commence_time",
                    (sport_key, now_str),
                ) as cur:
                    events = [dict(row) for row in await cur.fetchall()]

            ws._cached_real_events = events

            if not events:
                sport_name = REAL_SPORTS.get(sport_key, sport_key)
                return await interaction.followup.send(
                    f"No **{sport_name}** games available right now.\n"
                    f"Odds sync on a schedule \u2014 check back later!",
                    ephemeral=True,
                )

            sport_name = REAL_SPORTS.get(sport_key, sport_key)
            emoji = SPORT_EMOJI.get(sport_key, "\U0001f3c6")

            embed = discord.Embed(
                title=f"{emoji} {sport_name} \u2014 Upcoming Games",
                description=f"**{len(events)}** games available for betting.",
                color=TSL_GOLD,
            )

            lines = []
            for ev in events[:10]:
                ct = _parse_commence(ev["commence_time"])
                ts = f"<t:{int(ct.timestamp())}:R>" if ct else "TBD"
                lines.append(f"**{ev['away_team']}** @ **{ev['home_team']}** \u2014 {ts}")
            embed.add_field(name="Games", value="\n".join(lines) or "No games scheduled right now.", inline=False)

            options = []
            for ev in events[:25]:
                ct = _parse_commence(ev["commence_time"])
                time_str = ct.strftime("%m/%d %I:%M %p") if ct else "TBD"
                label = f"{ev['away_team']} @ {ev['home_team']}"
                if len(label) > 100:
                    label = label[:97] + "..."
                options.append(discord.SelectOption(label=label, value=ev["event_id"], description=time_str))

            sel = discord.ui.Select(placeholder="Select a game to bet on...", options=options, row=1)
            sel.callback = ws._on_real_game_select
            ws.add_item(sel)

            back = discord.ui.Button(label="\u2190 Sports", style=discord.ButtonStyle.secondary, row=2)
            back.callback = ws._back_to_sports
            ws.add_item(back)
            ws._add_cart_buttons(row=2)

            embed.set_footer(text=ws._cart_footer())
            await interaction.followup.send(embed=embed, view=ws, ephemeral=True)

    @classmethod
    async def open_to_parlay(cls, interaction: discord.Interaction, cog):
        """Create workspace in parlay-building mode.

        Every bet type click adds a leg directly — no wager screen shown.
        Called from the 'Build Parlay' hub button.
        interaction must already be deferred.
        """
        ws = cls(cog, interaction.user.id)
        ws._parlay_mode = True
        await ws.show_sport_selector(interaction, is_initial=True)

    # ── Callback factories ──────────────────────────────────────────────────

    def _make_sport_cb(self, sport_id: str):
        """Factory: sport button \u2192 loads game list for that sport."""
        async def callback(interaction: discord.Interaction):
            if sport_id == "tsl":
                await interaction.response.defer()
                try:
                    self._cached_tsl_games = await _load_tsl_week_games()
                    await self.show_tsl_games(interaction)
                except ValueError as e:
                    msg = str(e)
                    if "No game data loaded" in msg:
                        msg = "\u23f3 Game data is still loading. Try again in ~30 seconds."
                    embed = discord.Embed(description=f"\u274c {msg}", color=TSL_GOLD)
                    await self._update_workspace(interaction, embed, self)
                except Exception as e:
                    embed = discord.Embed(description=f"\u274c Error: `{e}`", color=TSL_GOLD)
                    await self._update_workspace(interaction, embed, self)
            else:
                await interaction.response.defer()
                sport_key = _SPORT_KEY_MAP.get(sport_id)
                if sport_key:
                    await self.show_real_games(interaction, sport_key)
        return callback

    async def _on_tsl_game_select(self, interaction: discord.Interaction):
        """User selected a TSL game from dropdown."""
        await interaction.response.defer()
        selected_id = interaction.data["values"][0]
        game = next(g for g in self._cached_tsl_games if str(g["game_id"]) == selected_id)
        await self.show_tsl_match(interaction, game)

    def _make_bet_cb(self, game: dict, pick: str, line, odds: int, bet_type: str):
        """Factory: TSL bet button \u2192 opens wager presets (or adds leg directly in parlay mode)."""
        async def callback(interaction: discord.Interaction):
            if _is_locked(game["game_id"]):
                embed = discord.Embed(description="\U0001f534 Game is **locked**.", color=TSL_GOLD)
                await self._update_workspace(interaction, embed, self)
                return
            game["_source_label"] = "TSL"
            if self._parlay_mode:
                await self._add_leg_direct(interaction, game, pick, line, odds, bet_type)
            else:
                await self.show_wager(interaction, game, pick, line, odds, bet_type)
        return callback

    def _make_real_bet_cb(self, event: dict, pick: str, bet_type: str, odds: int, line: float | None):
        """Factory: real sport bet button \u2192 opens wager presets."""
        async def callback(interaction: discord.Interaction):
            sport_key = self._current_sport_key
            source_label = REAL_SPORTS.get(sport_key, sport_key.split("_")[-1].upper())

            game_proxy = {
                "game_id": event["event_id"],
                "event_id": event["event_id"],
                "away": event["away_team"],
                "home": event["home_team"],
                "away_team": event["away_team"],
                "home_team": event["home_team"],
                "matchup_key": f"{event['away_team']} @ {event['home_team']}",
                "bet_week": dm.CURRENT_WEEK + 1,
                "_source_label": source_label,
                "_is_real": True,
                "_sport_key": sport_key,
            }
            if self._parlay_mode:
                await self._add_leg_direct(interaction, game_proxy, pick, line, odds, bet_type)
            else:
                await self.show_wager(interaction, game_proxy, pick, line, odds, bet_type)
        return callback

    def _make_place_bet_cb(self, amount: int):
        """Factory: preset amount button \u2192 places straight bet."""
        async def callback(interaction: discord.Interaction):
            bet = self._pending_bet
            game = bet["game"]

            await interaction.response.defer(ephemeral=True)
            try:
                if game.get("_is_real"):
                    # Re-fetch current odds — may have shifted since match detail loaded
                    fresh_odds = bet["odds"]  # fallback if DB unavailable
                    try:
                        async with aiosqlite.connect(DB_PATH) as _db:
                            _db.row_factory = aiosqlite.Row
                            async with _db.execute(
                                "SELECT * FROM real_events "
                                "WHERE event_id = ? AND locked = 0 AND completed = 0",
                                (game["event_id"],),
                            ) as _cur:
                                _fresh = await _cur.fetchone()
                        if _fresh is None:
                            await interaction.followup.send(
                                "❌ This game is no longer available for betting. Check current games from the sportsbook hub.",
                                ephemeral=True,
                            )
                            return
                        _fresh = dict(_fresh)
                        _home = game.get("home_team", game.get("home", ""))
                        _bt   = bet["bet_type"]
                        _pick = bet["pick"]
                        _col  = {
                            ("Moneyline", True):  "moneyline_home",
                            ("Moneyline", False): "moneyline_away",
                            ("Spread",    True):  "spread_home_odds",
                            ("Spread",    False): "spread_away_odds",
                            ("Over",      True):  "over_odds",
                            ("Over",      False): "over_odds",
                            ("Under",     True):  "under_odds",
                            ("Under",     False): "under_odds",
                        }.get((_bt, _pick == _home))
                        if _col and _fresh.get(_col) is not None:
                            fresh_odds = int(_fresh[_col])
                    except Exception:
                        pass  # fall back to cached odds on DB error
                    await _place_real_bet(
                        interaction, game, bet["bet_type"], bet["pick"],
                        fresh_odds, bet.get("line"), amount,
                        game.get("_source_label", "REAL"),
                    )
                else:
                    bet_week = game.get("bet_week", dm.CURRENT_WEEK + 1)
                    await _place_straight_bet(
                        interaction, team=bet["pick"], line=bet["line"], odds=bet["odds"],
                        game_id=game["game_id"], bet_type=bet["bet_type"],
                        matchup_key=game["matchup_key"],
                        away_name=game["away"], home_name=game["home"],
                        bet_week=bet_week, amount=amount, already_deferred=True,
                    )
            except flow_wallet.InsufficientFundsError:
                balance = _get_balance(interaction.user.id)
                await interaction.followup.send(
                    f"\u274c Insufficient balance. You have **${balance:,}**.", ephemeral=True
                )
        return callback

    async def _add_to_parlay_cb(self, interaction: discord.Interaction):
        """Add current selection to parlay cart, then return to game list."""
        bet = self._pending_bet
        game = bet["game"]

        leg = {
            "source": game.get("_source_label", "TSL"),
            "event_id": game.get("game_id", game.get("event_id", "")),
            "display": f"{game.get('away', game.get('away_team', ''))} @ {game.get('home', game.get('home_team', ''))}",
            "pick": bet["pick"],
            "bet_type": bet["bet_type"],
            "line": bet.get("line", 0),
            "odds": bet["odds"],
        }

        result = _add_to_cart(self.user_id, leg)
        if result == -1:
            embed = discord.Embed(
                description="\u26a0\ufe0f You already have a leg from this game in your cart.", color=TSL_GOLD
            )
            embed.set_footer(text=self._cart_footer())
            await self._update_workspace(interaction, embed, self)
            return
        if result == -2:
            embed = discord.Embed(
                description=f"\u26a0\ufe0f Cart is full \u2014 max **{MAX_PARLAY_LEGS}** legs.", color=TSL_GOLD
            )
            embed.set_footer(text=self._cart_footer())
            await self._update_workspace(interaction, embed, self)
            return

        # Stay on game list for same sport — cart footer shows updated leg count
        if self._current_event:
            await self.show_real_games(interaction, self._current_sport_key)
        else:
            await self.show_tsl_games(interaction)

    async def _add_leg_direct(self, interaction: discord.Interaction,
                               game: dict, pick: str, line, odds: int, bet_type: str):
        """Parlay mode: add leg immediately without showing the wager screen."""
        leg = {
            "source": game.get("_source_label", "TSL"),
            "event_id": game.get("game_id", game.get("event_id", "")),
            "display": (
                f"{game.get('away', game.get('away_team', ''))}"
                f" @ {game.get('home', game.get('home_team', ''))}"
            ),
            "pick": pick,
            "bet_type": bet_type,
            "line": line or 0,
            "odds": odds,
        }
        result = _add_to_cart(self.user_id, leg)
        if result == -1:
            embed = discord.Embed(
                description="\u26a0\ufe0f You already have a leg from this game in your cart.", color=TSL_GOLD
            )
            embed.set_footer(text=self._cart_footer())
            await self._update_workspace(interaction, embed, self)
            return
        if result == -2:
            embed = discord.Embed(
                description=f"\u26a0\ufe0f Cart is full \u2014 max **{MAX_PARLAY_LEGS}** legs.", color=TSL_GOLD
            )
            embed.set_footer(text=self._cart_footer())
            await self._update_workspace(interaction, embed, self)
            return
        # Stay on game list — cart footer shows the updated leg count
        if game.get("_is_real"):
            await self.show_real_games(interaction, self._current_sport_key)
        else:
            await self.show_tsl_games(interaction)

    async def _submit_parlay_cb(self, interaction: discord.Interaction):
        """Route to parlay review screen before opening wager modal."""
        await self.show_parlay_review(interaction)

    async def _clear_cart_cb(self, interaction: discord.Interaction):
        """Show confirmation before clearing the parlay cart."""
        cart = _get_cart(self.user_id)
        n = len(cart)
        self.clear_items()

        confirm_btn = discord.ui.Button(
            label=f"Yes, clear {n} leg{'s' if n != 1 else ''}",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=0,
        )

        async def _do_clear(i: discord.Interaction):
            _clear_cart(self.user_id)
            await self.show_sport_selector(i)

        async def _do_cancel(i: discord.Interaction):
            await self.show_sport_selector(i)

        confirm_btn.callback = _do_clear
        cancel_btn.callback = _do_cancel
        self.add_item(confirm_btn)
        self.add_item(cancel_btn)

        embed = discord.Embed(
            description=f"\u26a0\ufe0f Clear all **{n}** leg{'s' if n != 1 else ''} from your parlay cart? This can't be undone.",
            color=discord.Color.orange(),
        )
        await self._update_workspace(interaction, embed, self)

    async def _custom_wager_cb(self, interaction: discord.Interaction):
        """Open custom wager modal (straight or real bet)."""
        bet = self._pending_bet
        game = bet["game"]

        if game.get("_is_real"):
            modal = CustomRealWagerModal(
                game, bet["bet_type"], bet["pick"], bet["odds"], bet.get("line"),
            )
        else:
            bet_week = game.get("bet_week", dm.CURRENT_WEEK + 1)
            modal = CustomWagerModal(
                team=bet["pick"], line=bet["line"], odds=bet["odds"],
                game_id=game["game_id"], bet_type=bet["bet_type"],
                matchup_key=game["matchup_key"],
                away_name=game["away"], home_name=game["home"],
                bet_week=bet_week,
            )
        await interaction.response.send_modal(modal)

    # ── Navigation callbacks ────────────────────────────────────────────────

    async def _back_to_sports(self, interaction: discord.Interaction):
        """Dismiss the workspace — user returns to the hub card."""
        await interaction.response.edit_message(
            content="-# Sportsbook workspace closed. Run `/sportsbook` to reopen.",
            embed=None,
            view=None,
            attachments=[],
        )

    async def _back_to_tsl_games(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.show_tsl_games(interaction)

    async def _back_to_match(self, interaction: discord.Interaction):
        """Navigate back to the appropriate match detail (TSL or real)."""
        await interaction.response.defer()
        game = self._pending_bet.get("game", {})
        if game.get("_is_real"):
            await self.show_real_match(interaction, self._current_event)
        else:
            await self.show_tsl_match(interaction, self._current_game)

    async def _nav_real_games(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.show_real_games(interaction, self._current_sport_key)

    async def _on_real_game_select(self, interaction: discord.Interaction):
        """User selected a real sport game from dropdown."""
        await interaction.response.defer()
        event_id = interaction.data["values"][0]
        event = next((e for e in self._cached_real_events if e["event_id"] == event_id), None)
        if not event:
            embed = discord.Embed(description="Event not found.", color=TSL_GOLD)
            await self._update_workspace(interaction, embed, self)
            return
        await self.show_real_match(interaction, event)


# ═════════════════════════════════════════════════════════════════════════════
#  UNIFIED SPORTSBOOK HUB VIEW — TSL + Real Sports
# ═════════════════════════════════════════════════════════════════════════════

class SportsbookHubView(discord.ui.View):
    """Unified sportsbook hub — TSL simulation + real sports."""

    def __init__(self, cog: "SportsbookCog", user_id: int | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        # Update Cart badge with live count for this user
        if user_id is not None:
            try:
                import sqlite3 as _sq3
                with _sq3.connect(DB_PATH) as _c:
                    n = _c.execute(
                        "SELECT COUNT(*) FROM parlay_cart WHERE discord_id = ?", (user_id,)
                    ).fetchone()[0]
                if n > 0:
                    for item in self.children:
                        if getattr(item, "custom_id", None) == "atlas:sportsbook:parlay":
                            item.label = f"Cart [{n}]"
                            item.style = discord.ButtonStyle.success
                            break
            except Exception:
                pass  # non-critical; fallback to default "Cart" label

    # ── Row 0: US sports (TSL hero + big 3) ────────────────────────────────

    @discord.ui.button(label="TSL", emoji="\U0001f3c8",
                       style=discord.ButtonStyle.primary,
                       custom_id="atlas:sportsbook:tsl", row=0)
    async def tsl_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await SportsbookWorkspace.open_to_sport(interaction, self.cog, "tsl")
        except ValueError as e:
            msg = str(e)
            if "No game data loaded" in msg:
                msg = "\u23f3 Game data is still loading from the API. Please try again in ~30 seconds."
            else:
                msg = f"\u274c {msg}"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"\u274c Error loading TSL games: `{e}`", ephemeral=True)

    @discord.ui.button(label="NBA", emoji="\U0001f3c0",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:nba", row=0)
    async def nba_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "basketball_nba")

    @discord.ui.button(label="MLB", emoji="\u26be",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:mlb", row=0)
    async def mlb_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "baseball_mlb")

    @discord.ui.button(label="NHL", emoji="\U0001f3d2",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:nhl", row=0)
    async def nhl_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "icehockey_nhl")

    # ── Row 1: College, combat, international ────────────────────────────

    @discord.ui.button(label="NCAAB", emoji="\U0001f3c0",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:ncaab", row=1)
    async def ncaab_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "basketball_ncaab")

    @discord.ui.button(label="UFC", emoji="\U0001f94a",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:ufc", row=1)
    async def ufc_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "mma_ufc")

    @discord.ui.button(label="EPL", emoji="\u26bd",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:epl", row=1)
    async def epl_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "soccer_epl")

    @discord.ui.button(label="MLS", emoji="\u26bd",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:mls", row=1)
    async def mls_games(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_real_sport(interaction, "soccer_mls")

    # ── Row 2: Utilities (build → view → track → rank → stats) ──────────

    @discord.ui.button(label="Parlay", emoji="\U0001f3b0",
                       style=discord.ButtonStyle.primary,
                       custom_id="atlas:sportsbook:build_parlay", row=2)
    async def build_parlay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await SportsbookWorkspace.open_to_parlay(interaction, self.cog)
        except Exception as e:
            await interaction.followup.send(f"\u274c Error: `{e}`", ephemeral=True)

    @discord.ui.button(label="Cart", emoji="\U0001f6d2",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:parlay", row=2)
    async def parlay(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        cart = _get_cart(uid)
        if not cart:
            return await interaction.response.send_message(
                "🎰 Your **parlay cart** is empty!\n"
                "Place bets on individual games and choose **Add to Parlay** to build legs.",
                ephemeral=True,
            )
        try:
            combined = _combine_parlay_odds([leg["odds"] for leg in cart])
            embed = discord.Embed(
                title="🎰  PARLAY CART",
                color=TSL_GOLD,
                description=f"**{len(cart)} Leg{'s' if len(cart) != 1 else ''}** — Combined: **{combined:+d}**",
            )
            for i, leg in enumerate(cart, 1):
                source_badge = f"[{leg.get('source', 'TSL')}] " if leg.get('source') != 'TSL' else ""
                embed.add_field(
                    name=f"Leg {i}: {source_badge}{leg['pick']}",
                    value=f"{leg['bet_type']} ({leg['odds']:+d})\n{leg.get('display', '')}",
                    inline=False,
                )
            view = ParlayCartView(uid, cart, combined)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    @discord.ui.button(label="Active", emoji="\U0001f4cb",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:my_bets", row=2)
    async def my_bets(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog._mybets_impl(interaction)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"\u274c Error: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"\u274c Error: `{e}`", ephemeral=True)

    @discord.ui.button(label="Ranks", emoji="\U0001f3c6",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:leaderboard", row=2)
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog._leaderboard_impl(interaction)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    @discord.ui.button(label="My Stats", emoji="\U0001f4ca",
                       style=discord.ButtonStyle.secondary,
                       custom_id="atlas:sportsbook:stats", row=2)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            theme_id = get_theme_for_render(interaction.user.id)
            img = await build_stats_card(interaction.user.id, theme_id=theme_id)
            await send_card(interaction, img, filename="stats.png", followup=True, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    # ── Internal: real sport drill-down ────────────────────────────────────

    async def _show_real_sport(self, interaction: discord.Interaction, sport_key: str):
        """Delegate real sport drill-down to the workspace."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Reverse-map full sport_key to short sport_id for the workspace
        _REVERSE_SPORT = {v: k for k, v in _SPORT_KEY_MAP.items() if v is not None}
        sport_id = _REVERSE_SPORT.get(sport_key, sport_key)
        try:
            await SportsbookWorkspace.open_to_sport(interaction, self.cog, sport_id)
        except Exception as e:
            await interaction.followup.send(f"\u274c Error loading games: `{e}`", ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
#  TSL GAME SELECTOR (drill-down from hub TSL button)
# ═════════════════════════════════════════════════════════════════════════════

class SportsbookSelectView(discord.ui.View):
    def __init__(self, games: list[dict], cog: "SportsbookCog"):
        super().__init__(timeout=None)
        self.games = games
        self.cog   = cog

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
        is_locked = _is_locked(game["game_id"])

        await interaction.response.defer(ephemeral=True)

        from sportsbook_cards import build_match_detail_card
        theme_id = get_theme_for_render(interaction.user.id)
        png = await build_match_detail_card(game, locked=is_locked, theme_id=theme_id)

        await send_card(interaction, png, filename=f"match_{game['game_id']}.png",
                        followup=True, ephemeral=True, view=GameCardViewWithParlay(game))



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


def _derive_historical_leg_status(parlay_status: str) -> str:
    """Map parlay-level status to individual leg status for historical backfill."""
    if parlay_status == "Won":
        return "Won"
    if parlay_status in ("Cancelled",):
        return "Cancelled"
    if parlay_status == "Pending":
        return "Pending"
    # Lost, Push, Error — can't determine individual leg outcomes
    return "Unknown"


def get_parlay_display_info(parlay_id: str) -> tuple[int, str, list[dict]]:
    """Return (leg_count, odds_str, leg_picks) for a parlay. Used by highlight cards."""
    with _db_con() as con:
        leg_rows = con.execute(
            "SELECT pick, bet_type, line, status FROM parlay_legs "
            "WHERE parlay_id=? ORDER BY leg_index",
            (parlay_id,)
        ).fetchall()
        row = con.execute(
            "SELECT combined_odds FROM parlays_table WHERE parlay_id=?", (parlay_id,)
        ).fetchone()
    odds_str = f"{int(row[0]):+d}" if row else ""
    leg_picks = [{"pick": r[0], "bet_type": r[1], "line": r[2], "status": r[3]} for r in leg_rows]
    return len(leg_rows), odds_str, leg_picks


def backfill_parlay_legs_sync() -> int:
    """Populate parlay_legs from existing JSON legs column. Returns rows inserted."""
    count = 0
    with _db_con() as con:
        rows = con.execute(
            "SELECT parlay_id, legs, status FROM parlays_table WHERE legs IS NOT NULL"
        ).fetchall()

        batch = 0
        for parlay_id, legs_json, parlay_status in rows:
            existing = con.execute(
                "SELECT 1 FROM parlay_legs WHERE parlay_id=? LIMIT 1", (parlay_id,)
            ).fetchone()
            if existing:
                continue
            try:
                legs = json.loads(legs_json) if isinstance(legs_json, str) else []
            except Exception:
                log.warning("Corrupt parlay JSON in backfill: pid=%s", parlay_id)
                continue
            for i, leg in enumerate(legs):
                leg_status = _derive_historical_leg_status(parlay_status)
                con.execute(
                    "INSERT OR IGNORE INTO parlay_legs "
                    "(parlay_id, leg_index, game_id, matchup, pick, bet_type, line, odds, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (parlay_id, i, leg.get("game_id", ""),
                     leg.get("matchup", ""), leg.get("pick", ""),
                     leg.get("bet_type", ""), leg.get("line", 0),
                     leg.get("odds", 0), leg_status),
                )
                count += 1
            batch += 1
            if batch % 100 == 0:
                con.commit()
        con.commit()
    return count


# ═════════════════════════════════════════════════════════════════════════════
#  THE COG
# ═════════════════════════════════════════════════════════════════════════════

class SportsbookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        setup_db()
        # Set up balance snapshots table (for sparklines & weekly deltas)
        setup_snapshots_table()
        dm._autograde_callback = self._on_data_refresh
        _expire_stale_cart_legs()
        self.daily_snapshot.start()

    def cog_unload(self):
        self.daily_snapshot.cancel()
        dm._autograde_callback = None

    @tasks.loop(hours=24)
    async def daily_snapshot(self):
        """Take a daily balance snapshot for sparklines and weekly deltas."""
        await asyncio.to_thread(take_daily_snapshot)
        await asyncio.to_thread(_expire_stale_cart_legs)

    @daily_snapshot.before_loop
    async def before_daily_snapshot(self):
        await self.bot.wait_until_ready()
        # Run backfill on first startup if snapshots table is empty
        try:
            import sqlite3
            with sqlite3.connect(DB_PATH) as con:
                count = con.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]
            if count == 0:
                await asyncio.to_thread(backfill_from_bets)
        except Exception:
            pass  # Table might not exist yet on very first run

    async def _on_data_refresh(self):
        _invalidate_elo_cache()

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

    @app_commands.command(name="sportsbook", description="Open the ATLAS Sportsbook \u2014 TSL + Real Sports")
    async def sportsbook(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            theme_id = get_theme_for_render(interaction.user.id)
            img = await build_sportsbook_card(interaction.user.id, theme_id=theme_id)
            await send_card(interaction, img, filename="sportsbook.png",
                            followup=True, view=SportsbookHubView(self, interaction.user.id))
        except Exception as e:
            # Fallback to text embed if card rendering fails
            balance = _get_balance(interaction.user.id)
            embed = discord.Embed(title="\U0001f3c6  ATLAS GLOBAL SPORTSBOOK", color=TSL_GOLD)
            embed.description = (
                f"\U0001f4b0 **Balance:** ${balance:,}\n\n"
                f"Select a sport below to browse games.\n\n"
                f"*Card render error: `{e}`*"
            )
            embed.set_footer(text=f"ATLAS Sportsbook {SPORTSBOOK_VERSION}")
            await interaction.followup.send(embed=embed, view=SportsbookHubView(self, interaction.user.id))

    # ── User-facing _impl methods (called by board buttons) ────────────────

    async def _mybets_impl(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
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
                "SELECT parlay_id, week, combined_odds, wager_amount, status "
                "FROM parlays_table WHERE discord_id=? AND status='Pending' ORDER BY rowid DESC",
                (uid,)
            ).fetchall()
            # Batch-fetch legs from normalized parlay_legs table
            legs_map: dict[str, list[dict]] = {}
            if parlays:
                pids = [p[0] for p in parlays]
                placeholders = ",".join("?" * len(pids))
                leg_rows = con.execute(
                    f"SELECT parlay_id, pick FROM parlay_legs "
                    f"WHERE parlay_id IN ({placeholders}) ORDER BY parlay_id, leg_index",
                    pids,
                ).fetchall()
                for parlay_id, pick in leg_rows:
                    legs_map.setdefault(parlay_id, []).append({"pick": pick})

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
                    pid, week, c_odds, amt, status = p
                    legs = legs_map.get(pid, [])
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
                       "Tap any bet line, then **🎰 Add to Parlay**."),
                inline=False
            )
        embed.set_footer(text="TSL Sportsbook — Pending bets only")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _bethistory_impl(self, interaction: discord.Interaction, weeks: int = 99):
        await interaction.response.defer(thinking=True, ephemeral=True)
        uid = interaction.user.id

        with _db_con() as con:
            bets = con.execute(
                "SELECT matchup, bet_type, pick, wager_amount, odds, status, week "
                "FROM bets_table WHERE discord_id=? AND week >= ? ORDER BY week DESC, bet_id DESC",
                (uid, max(1, dm.CURRENT_WEEK - weeks))
            ).fetchall()
            parlays = con.execute(
                "SELECT parlay_id, week, combined_odds, wager_amount, status "
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
            pid, week, c_odds, amt, status = p
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

    async def _leaderboard_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        with _db_con() as con:
            users = con.execute(
                "SELECT discord_id, balance, season_start_balance "
                "FROM users_table ORDER BY balance DESC"
            ).fetchall()

        if not users:
            return await interaction.followup.send("No bettors found yet.", ephemeral=True)

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
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _props_impl(self, interaction: discord.Interaction):
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
    # ADMIN — OVERVIEW
    # ─────────────────────────────────────────────────────────────────────────

    async def _sb_status_impl(self, interaction: discord.Interaction):
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

    # ── Autograde status (boss panel) ────────────────────────────────────
    async def _autograde_status_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        h = _autograde_health
        with _db_con() as con:
            pending_tsl = con.execute(
                "SELECT COUNT(*) FROM bets_table WHERE status NOT IN ('Won','Lost','Push','Cancelled','Error')"
            ).fetchone()[0]
        try:
            import aiosqlite
            async with aiosqlite.connect(DB_PATH) as db:
                row = await db.execute("SELECT COUNT(*) FROM real_bets WHERE status='Pending'")
                pending_espn = (await row.fetchone())[0]
        except Exception:
            pending_espn = 0

        last = h["last_run_at"]
        last_str = f"<t:{int(last.timestamp())}:R>" if last else "Never"
        embed = discord.Embed(title="Autograde Health", color=TSL_GOLD)
        embed.add_field(name="Last Run", value=last_str, inline=True)
        embed.add_field(name="Duration", value=f"{h['last_run_duration_s']:.1f}s", inline=True)
        embed.add_field(name="Total Runs", value=str(h["total_runs"]), inline=True)
        embed.add_field(name="Last Settled", value=str(h["last_run_settled"]), inline=True)
        embed.add_field(name="Last Skipped", value=str(h["last_run_skipped"]), inline=True)
        embed.add_field(name="Total Settled", value=str(h["total_settled"]), inline=True)
        embed.add_field(name="Pending (TSL)", value=str(pending_tsl), inline=True)
        embed.add_field(name="Pending (ESPN)", value=str(pending_espn), inline=True)
        embed.add_field(name="Failures", value=str(h["consecutive_failures"]), inline=True)
        embed.add_field(name="Cycle", value="10 min (TSL + ESPN)", inline=True)
        embed.set_footer(text="TSL Sportsbook • Autograde Monitor")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Open bets browser (boss panel) ────────────────────────────────
    async def _open_bets_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        # Query TSL pending bets
        with _db_con() as con:
            tsl_bets = con.execute(
                "SELECT bet_id, discord_id, week, matchup, bet_type, pick, wager_amount, odds, created_at "
                "FROM bets_table WHERE status NOT IN ('Won','Lost','Push','Cancelled','Error') "
                "ORDER BY created_at DESC"
            ).fetchall()

        # Query ESPN pending bets
        espn_bets = []
        try:
            import sqlite3
            with sqlite3.connect(DB_PATH) as econ:
                espn_bets = econ.execute(
                    "SELECT b.bet_id, b.discord_id, 0, "
                    "COALESCE(e.away_team || ' @ ' || e.home_team, b.event_id), "
                    "b.bet_type, b.pick, b.wager_amount, b.odds, b.created_at "
                    "FROM real_bets b LEFT JOIN real_events e ON b.event_id = e.event_id "
                    "WHERE b.status='Pending' ORDER BY b.created_at DESC"
                ).fetchall()
        except Exception:
            pass

        all_bets = [(b, "TSL") for b in tsl_bets] + [(b, "ESPN") for b in espn_bets]
        if not all_bets:
            return await interaction.followup.send("No open bets.", ephemeral=True)

        PER_PAGE = 10
        total_pages = max(1, (len(all_bets) + PER_PAGE - 1) // PER_PAGE)

        def _build_page(page: int) -> discord.Embed:
            start = page * PER_PAGE
            chunk = all_bets[start:start + PER_PAGE]
            embed = discord.Embed(
                title=f"Open Bets ({len(all_bets)} total)",
                color=TSL_GOLD,
            )
            lines = []
            for bet, src in chunk:
                bid, uid, wk, matchup, btype, pick, wager, odds, created = bet
                wk_str = f"W{wk}" if wk else ""
                lines.append(
                    f"`#{bid}` **{src}** {wk_str} | {matchup}\n"
                    f"  {btype}: {pick} | ${wager:,} @ {odds}"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Page {page + 1}/{total_pages}")
            return embed

        from pagination_view import PaginationView
        embeds = [_build_page(i) for i in range(total_pages)]
        view = PaginationView(embeds, author_id=interaction.user.id)
        await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

    # ── Settled bets browser (boss panel) ─────────────────────────────
    async def _settled_bets_impl(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        # Query TSL settled bets
        with _db_con() as con:
            tsl_bets = con.execute(
                "SELECT bet_id, discord_id, week, matchup, bet_type, pick, wager_amount, odds, status, created_at "
                "FROM bets_table WHERE status IN ('Won','Lost','Push','Cancelled','Error') "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()

        # Query ESPN settled bets
        espn_bets = []
        try:
            import sqlite3
            with sqlite3.connect(DB_PATH) as econ:
                espn_bets = econ.execute(
                    "SELECT b.bet_id, b.discord_id, 0, "
                    "COALESCE(e.away_team || ' @ ' || e.home_team, b.event_id), "
                    "b.bet_type, b.pick, b.wager_amount, b.odds, b.status, b.created_at "
                    "FROM real_bets b LEFT JOIN real_events e ON b.event_id = e.event_id "
                    "WHERE b.status IN ('Won','Lost','Push','Cancelled') "
                    "ORDER BY b.created_at DESC LIMIT 200"
                ).fetchall()
        except Exception:
            pass

        _STATUS_EMOJI = {"Won": "✅", "Lost": "❌", "Push": "➖", "Cancelled": "🚫", "Error": "⚠️"}
        all_bets = [(b, "TSL") for b in tsl_bets] + [(b, "ESPN") for b in espn_bets]
        # Sort by created_at descending (index 9)
        all_bets.sort(key=lambda x: x[0][9] or "", reverse=True)

        if not all_bets:
            return await interaction.followup.send("No settled bets.", ephemeral=True)

        PER_PAGE = 10
        total_pages = max(1, (len(all_bets) + PER_PAGE - 1) // PER_PAGE)

        def _build_page(page: int) -> discord.Embed:
            start = page * PER_PAGE
            chunk = all_bets[start:start + PER_PAGE]
            embed = discord.Embed(
                title=f"Settled Bets (recent {len(all_bets)})",
                color=TSL_GOLD,
            )
            lines = []
            for bet, src in chunk:
                bid, uid, wk, matchup, btype, pick, wager, odds, status, created = bet
                emoji = _STATUS_EMOJI.get(status, "❓")
                payout_str = ""
                if status == "Won":
                    payout_str = f" → ${_payout_calc(wager, int(odds)):,}"
                elif status == "Push":
                    payout_str = f" → ${wager:,}"
                lines.append(
                    f"{emoji} `#{bid}` **{src}** | {matchup}\n"
                    f"  {btype}: {pick} | ${wager:,}{payout_str}"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Page {page + 1}/{total_pages}")
            return embed

        from pagination_view import PaginationView
        embeds = [_build_page(i) for i in range(total_pages)]
        view = PaginationView(embeds, author_id=interaction.user.id)
        await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)

    # ── Force-settle (boss panel) ─────────────────────────────────────
    async def _force_settle_impl(self, interaction: discord.Interaction, bet_id: int, result: str):
        result = result.strip().title()
        if result not in ("Won", "Lost", "Push", "Cancelled"):
            return await interaction.followup.send(
                f"❌ Invalid result `{result}`. Must be Won/Lost/Push/Cancelled.", ephemeral=True
            )

        with _db_con() as con:
            row = con.execute(
                "SELECT discord_id, matchup, bet_type, pick, wager_amount, odds, status "
                "FROM bets_table WHERE bet_id=?", (bet_id,)
            ).fetchone()
            if not row:
                return await interaction.followup.send(f"❌ Bet #{bet_id} not found.", ephemeral=True)

            uid, matchup, btype, pick, wager, odds, cur_status = row
            if cur_status in ("Won", "Lost", "Push", "Cancelled"):
                return await interaction.followup.send(
                    f"❌ Bet #{bet_id} already settled as **{cur_status}**.", ephemeral=True
                )

            payout = 0
            if result == "Won":
                payout = _payout_calc(wager, int(odds))
                _update_balance(uid, payout, con,
                                subsystem="TSL_BET", subsystem_id=str(bet_id),
                                reference_key=f"TSL_BET_{bet_id}_won_force")
            elif result == "Push":
                payout = wager
                _update_balance(uid, wager, con,
                                subsystem="TSL_BET", subsystem_id=str(bet_id),
                                reference_key=f"TSL_BET_{bet_id}_push_force")

            con.execute("UPDATE bets_table SET status=? WHERE bet_id=?", (result, bet_id))
            import wager_registry
            _ra = (payout - wager) if result == "Won" else (0 if result == "Push" else -wager)
            wager_registry.settle_wager_sync("TSL_BET", str(bet_id), result.lower(), _ra, con=con)
            new_bal = _get_balance(uid)

        # Post to #ledger
        try:
            from ledger_poster import post_bet_settlement
            guild = interaction.guild
            if guild:
                await post_bet_settlement(
                    self.bot, guild.id, uid, bet_id, matchup, btype, pick,
                    wager, result, payout, new_bal, source="TSL",
                )
        except Exception:
            log.exception("[FORCE-SETTLE] Failed to post to #ledger")

        await interaction.followup.send(
            f"✅ Bet `#{bet_id}` force-settled as **{result}**.\n"
            f"Matchup: {matchup} | {btype}: {pick}\n"
            f"Wager: ${wager:,} → Payout: ${payout:,} | New balance: ${new_bal:,}",
            ephemeral=True,
        )

    async def _sb_lines_impl(self, interaction: discord.Interaction):
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

        embed = discord.Embed(
            title=f"🔬  Line Debug — Week {bet_week}  (Elo Engine {SPORTSBOOK_VERSION})",
            color=AtlasColors.INFO
        )
        for g in ui_games[:8]:   # Discord embed field limit
            ov_note = " **[OVERRIDE]**" if g.get("_overridden") else ""
            h_elo = g.get("_home_elo", 1500)
            a_elo = g.get("_away_elo", 1500)
            h_pow = g.get("_home_power", 50)
            a_pow = g.get("_away_power", 50)

            embed.add_field(
                name=f"{g['away']} @ {g['home']}{ov_note}",
                value=(
                    f"Elo: **{g['away']}** {a_elo:.0f} vs **{g['home']}** {h_elo:.0f}\n"
                    f"Power: {a_pow:.1f} vs {h_pow:.1f}\n"
                    f"Spread: home {g['home_spread']} (engine {g['_engine_spread']:+.1f})\n"
                    f"ML: {g['away']} {g['away_ml']} / {g['home']} {g['home_ml']}\n"
                    f"O/U: {g['ou_line']}"
                ),
                inline=False
            )

        embed.set_footer(
            text=f"Elo Engine {SPORTSBOOK_VERSION}  •  League Avg: {_LEAGUE_AVG_SCORE:.1f} pts/team"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — LINE OVERRIDES
    # ─────────────────────────────────────────────────────────────────────────

    async def _sb_setspread_impl(self, interaction: discord.Interaction,
                                 matchup: str, home_spread: float):
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

    async def _sb_setml_impl(self, interaction: discord.Interaction,
                             matchup: str, home_ml: int, away_ml: int):
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

    async def _sb_setou_impl(self, interaction: discord.Interaction, matchup: str, ou_line: float):
        _set_line_override(
            matchup.strip(),
            set_by=interaction.user.display_name,
            ou_line=ou_line,
        )
        await interaction.response.send_message(
            f"✅ **O/U override set** for `{matchup}`: **{ou_line}**",
            ephemeral=True
        )

    async def _sb_resetlines_impl(self, interaction: discord.Interaction):
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

    async def _sb_lock_impl(self, interaction: discord.Interaction, matchup: str, locked: bool):
        _set_locked(matchup.strip(), locked)
        status = "🔴 **LOCKED**" if locked else "🟢 **UNLOCKED**"
        await interaction.response.send_message(
            f"{status} — `{matchup}` betting is now {'closed' if locked else 'open'}.",
            ephemeral=True
        )

    async def _sb_lockall_impl(self, interaction: discord.Interaction):
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

    async def _sb_unlockall_impl(self, interaction: discord.Interaction):
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

    async def _sb_cancelgame_impl(self, interaction: discord.Interaction, matchup: str):
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
                _update_balance(uid, amt, con,
                                subsystem="TSL_BET", subsystem_id=str(bid),
                                reference_key=f"TSL_BET_{bid}_cancel")
                con.execute(
                    "UPDATE bets_table SET status='Cancelled' WHERE bet_id=?", (bid,)
                )
                import wager_registry
                wager_registry.settle_wager_sync("TSL_BET", str(bid), "voided", 0, con=con)
                refunded      += 1
                total_refunded += amt

            # Also refund parlays containing this matchup (query normalized table)
            parlay_refunds = 0
            parlay_refund_users = set()  # collect UIDs for ledger posting outside this block
            affected_parlay_ids = con.execute(
                "SELECT DISTINCT parlay_id FROM parlay_legs "
                "WHERE LOWER(matchup) LIKE ? AND status='Pending'",
                (f"%{key}%",),
            ).fetchall()
            for (pid,) in affected_parlay_ids:
                row = con.execute(
                    "SELECT discord_id, wager_amount FROM parlays_table "
                    "WHERE parlay_id=? AND status='Pending'",
                    (pid,),
                ).fetchone()
                if not row:
                    continue
                uid, amt = row
                _update_balance(uid, amt, con,
                                subsystem="PARLAY", subsystem_id=str(pid),
                                reference_key=f"PARLAY_{pid}_cancel")
                con.execute(
                    "UPDATE parlays_table SET status='Cancelled' WHERE parlay_id=?", (pid,)
                )
                con.execute(
                    "UPDATE parlay_legs SET status='Cancelled' "
                    "WHERE parlay_id=? AND status='Pending'",
                    (pid,),
                )
                import wager_registry
                wager_registry.settle_wager_sync("PARLAY", str(pid), "voided", 0, con=con)
                parlay_refunds += 1
                total_refunded += amt
                parlay_refund_users.add(uid)

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

        # Post ledger slips for each refunded bet
        try:
            from ledger_poster import post_transaction
            refund_users = set()
            for bid, uid, amt in pending:
                refund_users.add(uid)
            for uid in parlay_refund_users:
                refund_users.add(uid)
            for uid in refund_users:
                bal = _get_balance(uid)
                txn_id = await flow_wallet.get_last_txn_id(uid)
                await post_transaction(
                    interaction.client, interaction.guild_id, uid,
                    "TSL_BET", 0, bal,
                    f"Refund: Game cancelled — {matchup}",
                    txn_id,
                )
        except Exception:
            log.exception("Ledger post failed for cancellation refund")

    async def _sb_refund_impl(self, interaction: discord.Interaction, bet_id: int):
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
            import wager_registry
            wager_registry.settle_wager_sync("TSL_BET", str(bet_id), "voided", 0, con=con)

        member = interaction.guild.get_member(uid) if interaction.guild else None
        name   = member.display_name if member else f"<@{uid}>"
        await interaction.response.send_message(
            f"✅ Refunded bet `#{bet_id}` — **{name}** gets **${amt:,}** back\n"
            f"*(was: {pick} {btype} on {matchup})*",
            ephemeral=True
        )

        # Post to #ledger
        try:
            bal = _get_balance(uid)
            txn_id = await flow_wallet.get_last_txn_id(uid)
            from ledger_poster import post_transaction
            await post_transaction(
                interaction.client, interaction.guild_id, uid,
                "TSL_BET", amt, bal,
                f"Refund: {pick} {btype} on {matchup} (bet #{bet_id})",
                txn_id,
            )
        except Exception:
            log.exception("Ledger post failed for bet refund #%s", bet_id)

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — BALANCE MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    async def _sb_balance_impl(self, interaction: discord.Interaction,
                               member: discord.Member,
                               adjustment: int,
                               reason: str = "Commissioner adjustment"):
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

        # Post to #ledger
        try:
            txn_id = await flow_wallet.get_last_txn_id(member.id)
            from ledger_poster import post_transaction
            await post_transaction(
                interaction.client, interaction.guild_id, member.id,
                "ADMIN", adjustment, new_balance, reason, txn_id,
            )
        except Exception:
            log.exception("Ledger post failed for admin adjustment")

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — PROP BET MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    async def _sb_addprop_impl(self, interaction: discord.Interaction,
                               description: str,
                               option_a: str,
                               option_b: str,
                               odds_a: int = -110,
                               odds_b: int = -110):
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

    async def _sb_settleprop_impl(self, interaction: discord.Interaction,
                                  prop_id: int, result: str):
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
                    _update_balance(uid, amt, con,
                                    subsystem="PROP", subsystem_id=str(wid),
                                    reference_key=f"PROP_PUSH_{wid}")
                    con.execute("UPDATE prop_wagers SET status='Push' WHERE id=?", (wid,))
                    import wager_registry
                    wager_registry.settle_wager_sync("PROP", str(wid), "push", 0, con=con)
                    pushes += 1
                else:
                    winning_pick = opt_a if result == "a" else opt_b
                    if pick_lower == winning_pick.lower().strip():
                        payout = _payout_calc(amt, int(odds))
                        _update_balance(uid, payout, con,
                                        subsystem="PROP", subsystem_id=str(wid),
                                        reference_key=f"PROP_SETTLE_{wid}")
                        total_paid += payout - amt
                        con.execute("UPDATE prop_wagers SET status='Won' WHERE id=?", (wid,))
                        import wager_registry
                        wager_registry.settle_wager_sync("PROP", str(wid), "won", payout - amt, con=con)
                        wins += 1
                    else:
                        flow_wallet.update_balance_sync(
                            uid, 0, source="TSL_BET",
                            description=f"Lost: Prop #{prop_id} — {pick}",
                            reference_key=f"PROP_SETTLE_{wid}",
                            con=con, subsystem="PROP", subsystem_id=str(wid),
                        )
                        con.execute("UPDATE prop_wagers SET status='Lost' WHERE id=?", (wid,))
                        import wager_registry
                        wager_registry.settle_wager_sync("PROP", str(wid), "lost", -amt, con=con)
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

        # Post ledger slips for prop settlement (wins/pushes)
        try:
            from ledger_poster import post_transaction
            for wid, uid, pick, amt, odds in wagers:
                pick_lower = pick.lower().strip()
                if result == "push":
                    bal = _get_balance(uid)
                    txn_id = await flow_wallet.get_last_txn_id(uid)
                    await post_transaction(
                        interaction.client, interaction.guild_id, uid,
                        "TSL_BET", amt, bal,
                        f"Push: Prop #{prop_id} — {desc[:40]}", txn_id,
                    )
                else:
                    winning_pick = opt_a if result == "a" else opt_b
                    if pick_lower == winning_pick.lower().strip():
                        payout = _payout_calc(amt, int(odds))
                        bal = _get_balance(uid)
                        txn_id = await flow_wallet.get_last_txn_id(uid)
                        await post_transaction(
                            interaction.client, interaction.guild_id, uid,
                            "TSL_BET", payout, bal,
                            f"Won: Prop #{prop_id} — {desc[:40]}", txn_id,
                        )
        except Exception:
            log.exception("Ledger post failed for prop resolution")

async def setup(bot: commands.Bot):
    cog = SportsbookCog(bot)
    await bot.add_cog(cog)
    # Persistent view: routes ALL atlas:sportsbook:* custom_ids to this instance
    bot.add_view(SportsbookHubView(cog))

"""
real_sportsbook_cog.py — ATLAS Flow: Real Sports Sportsbook
=============================================================
Bet on real sports with TSL Bucks using live odds from ESPN.

Supported leagues: NFL, NBA, MLB, NHL, NCAAB, UFC/MMA, EPL, MLS, WNBA

Background tasks:
  - Odds sync: every 15 minutes (in-season sports only)
  - Score sync: every 15 minutes (all sports)
  - Lock check: every 60 seconds

Author: TheWitt / ATLAS
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import discord
from discord.ext import commands, tasks

import flow_wallet
from flow_wallet import DB_PATH, InsufficientFundsError
from espn_odds import ESPNOddsClient, SUPPORTED_SPORTS, LEAGUE_MAP
from team_branding import TeamBranding

log = logging.getLogger("real_sportsbook")

# ── Config ────────────────────────────────────────────────────────────────────

MIN_BET = 50
DEFAULT_MAX_BET = 5000
from atlas_colors import AtlasColors
TSL_GOLD = AtlasColors.TSL_GOLD.value

# Sport-specific emoji
SPORT_EMOJI = {
    "americanfootball_nfl": "\U0001f3c8",  # 🏈
    "basketball_nba":       "\U0001f3c0",  # 🏀
    "baseball_mlb":         "\u26be",      # ⚾
    "icehockey_nhl":        "\U0001f3d2",  # 🏒
    "basketball_ncaab":     "\U0001f3c0",  # 🏀
    "mma_ufc":              "\U0001f94a",  # 🥊
    "soccer_epl":           "\u26bd",      # ⚽
    "soccer_mls":           "\u26bd",      # ⚽
    "basketball_wnba":      "\U0001f3c0",  # 🏀
}

# Season windows + sync schedule per sport
# months: which months the sport is in-season
# sync_days: weekday numbers (Mon=0 .. Sun=6) to fetch fresh odds
SPORT_SEASONS = {
    "americanfootball_nfl": {"months": {9, 10, 11, 12, 1, 2}},
    "basketball_nba":       {"months": {10, 11, 12, 1, 2, 3, 4, 5, 6}},
    "baseball_mlb":         {"months": {3, 4, 5, 6, 7, 8, 9, 10}},
    "icehockey_nhl":        {"months": {10, 11, 12, 1, 2, 3, 4, 5, 6}},
    "basketball_ncaab":     {"months": {11, 12, 1, 2, 3, 4}},
    "mma_ufc":              {"months": {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}},
    "soccer_epl":           {"months": {8, 9, 10, 11, 12, 1, 2, 3, 4, 5}},
    "soccer_mls":           {"months": {2, 3, 4, 5, 6, 7, 8, 9, 10, 11}},
    "basketball_wnba":      {"months": {5, 6, 7, 8, 9}},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

from odds_utils import american_to_str as _american_to_str, payout_calc as _payout_calc, profit_calc as _profit_calc  # noqa: E402


async def _get_max_bet() -> int:
    """Read per-event max wager from sportsbook_settings, default 5000."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT value FROM sportsbook_settings WHERE key = 'max_bet_real'"
            ) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row else DEFAULT_MAX_BET
    except Exception:
        return DEFAULT_MAX_BET


def _parse_commence(ct: str) -> Optional[datetime]:
    """Parse ISO8601 commence_time string."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(ct, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# ── Bet evaluation (module-level for reuse) ──────────────────────────────────

def _evaluate_bet(bet_type: str, pick: str, odds: int, line: float,
                  wager: int, home_team: str, away_team: str,
                  home_score: int, away_score: int) -> str:
    """Evaluate a single bet. Returns 'Won', 'Lost', or 'Push'."""
    total = home_score + away_score

    if bet_type == "Moneyline":
        if home_score == away_score:
            return "Push"
        if pick == home_team:
            return "Won" if home_score > away_score else "Lost"
        else:
            return "Won" if away_score > home_score else "Lost"

    elif bet_type == "Spread":
        if pick == home_team:
            adjusted = home_score + (line or 0)
            if adjusted > away_score:
                return "Won"
            elif adjusted == away_score:
                return "Push"
            return "Lost"
        else:
            adjusted = away_score + (line or 0)
            if adjusted > home_score:
                return "Won"
            elif adjusted == home_score:
                return "Push"
            return "Lost"

    elif bet_type == "Over":
        if line is None:
            return "Lost"
        if total > line:
            return "Won"
        elif total == line:
            return "Push"
        return "Lost"

    elif bet_type == "Under":
        if line is None:
            return "Lost"
        if total < line:
            return "Won"
        elif total == line:
            return "Push"
        return "Lost"

    return "Lost"


# ── Cross-sport parlay leg grading ───────────────────────────────────────────

async def _grade_parlay_legs_for_event(event_id: str, home_team: str,
                                        away_team: str, home_score: int,
                                        away_score: int):
    """Grade parlay legs tied to a real-sport event and settle if ready."""
    from flow_sportsbook import _check_parlay_completion, _db_con

    con = _db_con()
    try:
        legs = con.execute(
            "SELECT parlay_id, leg_index, pick, bet_type, line, odds "
            "FROM parlay_legs WHERE game_id = ? AND source != 'TSL' AND status = 'Pending'",
            (event_id,),
        ).fetchall()
        if not legs:
            return

        affected_parlays = set()
        for pid, leg_idx, pick, bet_type, line_val, odds in legs:
            result = _evaluate_bet(
                bet_type, pick, int(odds), float(line_val or 0),
                0,  # wager not needed for evaluation
                home_team, away_team, home_score, away_score,
            )
            con.execute(
                "UPDATE parlay_legs SET status = ? WHERE parlay_id = ? AND leg_index = ?",
                (result, pid, leg_idx),
            )
            affected_parlays.add(pid)

        con.commit()

        for pid in affected_parlays:
            _check_parlay_completion(pid, con=con)

        con.commit()
    finally:
        con.close()


# ── Cog ───────────────────────────────────────────────────────────────────────

class RealSportsbookCog(commands.Cog, name="RealSportsbookCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.branding = TeamBranding("assets/team_branding.json", "assets/player_headshots.json")
        self.client = ESPNOddsClient(branding=self.branding)
        self._ready = False

    async def _setup_tables(self):
        """Create real sportsbook tables with flattened odds schema."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS real_events (
                    event_id        TEXT PRIMARY KEY,
                    sport_key       TEXT NOT NULL,
                    sport_title     TEXT NOT NULL,
                    home_team       TEXT NOT NULL,
                    away_team       TEXT NOT NULL,
                    home_team_abbr  TEXT,
                    away_team_abbr  TEXT,
                    home_team_espn_id TEXT,
                    away_team_espn_id TEXT,
                    commence_time   TEXT,
                    home_score      INTEGER,
                    away_score      INTEGER,
                    locked          INTEGER DEFAULT 0,
                    completed       INTEGER DEFAULT 0,
                    -- Flattened odds (replaces real_odds table)
                    spread_home       REAL,
                    spread_away       REAL,
                    spread_home_odds  INTEGER,
                    spread_away_odds  INTEGER,
                    moneyline_home    INTEGER,
                    moneyline_away    INTEGER,
                    over_under        REAL,
                    over_odds         INTEGER,
                    under_odds        INTEGER,
                    win_prob_home     REAL,
                    win_prob_away     REAL,
                    odds_provider     TEXT DEFAULT 'Consensus',
                    -- Branding
                    home_color      TEXT,
                    away_color      TEXT,
                    home_logo_url   TEXT,
                    away_logo_url   TEXT,
                    -- Sync metadata
                    last_odds_sync  TEXT,
                    last_score_sync TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS real_bets (
                    bet_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id    INTEGER NOT NULL,
                    event_id      TEXT NOT NULL,
                    sport_key     TEXT,
                    bet_type      TEXT NOT NULL,
                    pick          TEXT NOT NULL,
                    odds          INTEGER NOT NULL,
                    line          REAL,
                    wager_amount  INTEGER NOT NULL,
                    status        TEXT DEFAULT 'Pending',
                    created_at    TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sportsbook_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.commit()

            # Migrate: add new columns to existing real_events if missing
            await self._migrate_schema(db)

        log.info("Real sportsbook tables ready.")

    async def _migrate_schema(self, db: aiosqlite.Connection):
        """Add new flat-odds columns to real_events if upgrading from old schema.
        Backfill from real_odds, then drop real_odds."""
        # Check if new columns already exist
        cursor = await db.execute("PRAGMA table_info(real_events)")
        columns = {row[1] for row in await cursor.fetchall()}

        new_cols = {
            "home_team_abbr": "TEXT",
            "away_team_abbr": "TEXT",
            "home_team_espn_id": "TEXT",
            "away_team_espn_id": "TEXT",
            "spread_home": "REAL",
            "spread_away": "REAL",
            "spread_home_odds": "INTEGER",
            "spread_away_odds": "INTEGER",
            "moneyline_home": "INTEGER",
            "moneyline_away": "INTEGER",
            "over_under": "REAL",
            "over_odds": "INTEGER",
            "under_odds": "INTEGER",
            "win_prob_home": "REAL",
            "win_prob_away": "REAL",
            "odds_provider": "TEXT",
            "home_color": "TEXT",
            "away_color": "TEXT",
            "home_logo_url": "TEXT",
            "away_logo_url": "TEXT",
        }

        added = 0
        # SAFETY: col/dtype are from the hardcoded new_cols dict above (DDL migration).
        for col, dtype in new_cols.items():
            if col not in columns:
                default = " DEFAULT 'Consensus'" if col == "odds_provider" else ""
                await db.execute(f"ALTER TABLE real_events ADD COLUMN {col} {dtype}{default}")
                added += 1

        if added:
            log.info(f"Migration: added {added} new columns to real_events.")

        # Check if old real_odds table exists → backfill + drop
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='real_odds'"
        )
        if await cursor.fetchone():
            log.info("Migration: backfilling odds from real_odds into real_events...")
            # For each event with pending bets, backfill the last known odds
            rows = await db.execute_fetchall("""
                SELECT DISTINCT re.event_id, re.home_team, re.away_team
                FROM real_events re
                JOIN real_bets rb ON rb.event_id = re.event_id
                WHERE rb.status = 'Pending' AND re.completed = 0
            """)
            for event_id, home, away in rows:
                # Moneyline
                ml_rows = await db.execute_fetchall(
                    "SELECT outcome_name, price FROM real_odds "
                    "WHERE event_id = ? AND market = 'h2h' ORDER BY last_updated DESC",
                    (event_id,),
                )
                ml_home = ml_away = None
                for name, price in ml_rows:
                    if name == home and ml_home is None:
                        ml_home = price
                    elif name == away and ml_away is None:
                        ml_away = price

                # Spread
                sp_rows = await db.execute_fetchall(
                    "SELECT outcome_name, price, point FROM real_odds "
                    "WHERE event_id = ? AND market = 'spreads' ORDER BY last_updated DESC",
                    (event_id,),
                )
                sp_home = sp_away = sp_home_odds = sp_away_odds = None
                for name, price, point in sp_rows:
                    if name == home and sp_home is None:
                        sp_home = point
                        sp_home_odds = price
                    elif name == away and sp_away is None:
                        sp_away = point
                        sp_away_odds = price

                # Totals
                tot_rows = await db.execute_fetchall(
                    "SELECT outcome_name, price, point FROM real_odds "
                    "WHERE event_id = ? AND market = 'totals' ORDER BY last_updated DESC",
                    (event_id,),
                )
                ou_total = over_odds = under_odds = None
                for name, price, point in tot_rows:
                    if name == "Over" and over_odds is None:
                        ou_total = point
                        over_odds = price
                    elif name == "Under" and under_odds is None:
                        under_odds = price

                await db.execute("""
                    UPDATE real_events SET
                        moneyline_home = ?, moneyline_away = ?,
                        spread_home = ?, spread_away = ?,
                        spread_home_odds = ?, spread_away_odds = ?,
                        over_under = ?, over_odds = ?, under_odds = ?
                    WHERE event_id = ?
                """, (ml_home, ml_away, sp_home, sp_away, sp_home_odds,
                      sp_away_odds, ou_total, over_odds, under_odds, event_id))

            await db.execute("DROP TABLE real_odds")
            await db.commit()
            log.info("Migration: dropped real_odds table. Flat schema active.")

    async def cog_load(self):
        await self._setup_tables()
        # Odds/scores sync is manual-only (via /boss Sportsbook → Sync All)
        # to avoid burning API quota during frequent dev restarts.
        self.lock_started_games.start()
        self._ready = True

    async def cog_unload(self):
        if self.sync_scores_task.is_running():
            self.sync_scores_task.cancel()
        self.lock_started_games.cancel()
        if self.sync_odds_task.is_running():
            self.sync_odds_task.cancel()
        await self.client.close()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # BACKGROUND TASKS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @tasks.loop(minutes=10)
    async def sync_scores_task(self):
        """Fetch scores every 10 minutes, auto-grade completed bets."""
        await asyncio.sleep(random.uniform(5, 15))
        try:
            await self._sync_scores()
        except Exception:
            log.exception("[REAL-SB] Score sync exception — will retry next cycle")

    @sync_scores_task.before_loop
    async def _before_scores(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def lock_started_games(self):
        """Lock events where commence_time <= now."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE real_events SET locked = 1 "
                    "WHERE locked = 0 AND completed = 0 AND commence_time <= ?",
                    (now,),
                )
                await db.commit()
        except Exception as e:
            log.error(f"Lock task error: {e}")

    @lock_started_games.before_loop
    async def _before_lock(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=15)
    async def sync_odds_task(self):
        """Sync odds for all in-season sports every 15 minutes."""
        await asyncio.sleep(random.uniform(5, 15))
        now = datetime.now(timezone.utc)
        for sport_key, cfg in SPORT_SEASONS.items():
            if now.month in cfg["months"]:
                await self._sync_odds(sport_key)

    @sync_odds_task.before_loop
    async def _before_odds(self):
        await self.bot.wait_until_ready()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ODDS SYNC
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _sync_odds(self, sport_key: str):
        """Fetch odds from ESPN and upsert into real_events (flat schema)."""
        league_info = LEAGUE_MAP.get(sport_key)
        if not league_info:
            log.warning(f"Unknown sport_key: {sport_key}")
            return

        league_key = league_info[2]  # "NFL", "NBA", etc.
        log.info(f"Syncing odds for {sport_key} ({league_key})...")

        try:
            games = await self.client.get_upcoming_odds(league_key)
        except Exception as e:
            log.error(f"ESPN odds fetch failed for {sport_key}: {e}")
            return

        if not games:
            log.warning(f"No upcoming games for {sport_key}.")
            return

        now = datetime.now(timezone.utc).isoformat()
        sport_title = SUPPORTED_SPORTS.get(sport_key, sport_key)
        upserted = 0

        async with aiosqlite.connect(DB_PATH) as db:
            for game in games:
                event_id = game.get("event_id", "")
                ht = game.get("home_team", {})
                at = game.get("away_team", {})
                home = ht.get("display_name", "")
                away = at.get("display_name", "")
                if not event_id or not home or not away:
                    continue

                spread = game.get("spread", {})
                ml = game.get("moneyline", {})
                ou = game.get("over_under", {})
                wp = game.get("win_probability", {})

                await db.execute("""
                    INSERT INTO real_events (
                        event_id, sport_key, sport_title, home_team, away_team,
                        home_team_abbr, away_team_abbr,
                        home_team_espn_id, away_team_espn_id,
                        commence_time,
                        spread_home, spread_away, spread_home_odds, spread_away_odds,
                        moneyline_home, moneyline_away,
                        over_under, over_odds, under_odds,
                        win_prob_home, win_prob_away, odds_provider,
                        home_color, away_color, home_logo_url, away_logo_url,
                        last_odds_sync
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        home_team = excluded.home_team,
                        away_team = excluded.away_team,
                        commence_time = excluded.commence_time,
                        spread_home = excluded.spread_home,
                        spread_away = excluded.spread_away,
                        spread_home_odds = excluded.spread_home_odds,
                        spread_away_odds = excluded.spread_away_odds,
                        moneyline_home = excluded.moneyline_home,
                        moneyline_away = excluded.moneyline_away,
                        over_under = excluded.over_under,
                        over_odds = excluded.over_odds,
                        under_odds = excluded.under_odds,
                        win_prob_home = excluded.win_prob_home,
                        win_prob_away = excluded.win_prob_away,
                        odds_provider = excluded.odds_provider,
                        home_color = excluded.home_color,
                        away_color = excluded.away_color,
                        home_logo_url = excluded.home_logo_url,
                        away_logo_url = excluded.away_logo_url,
                        last_odds_sync = excluded.last_odds_sync
                """, (
                    event_id, sport_key, sport_title, home, away,
                    ht.get("abbreviation"), at.get("abbreviation"),
                    ht.get("espn_id"), at.get("espn_id"),
                    game.get("event_date", ""),
                    spread.get("home"), spread.get("away"),
                    spread.get("home_odds"), spread.get("away_odds"),
                    ml.get("home"), ml.get("away"),
                    ou.get("total"), ou.get("over_odds"), ou.get("under_odds"),
                    wp.get("home"), wp.get("away"),
                    spread.get("provider", "Consensus"),
                    ht.get("color"), at.get("color"),
                    ht.get("logo_url"), at.get("logo_url"),
                    now,
                ))
                upserted += 1

            await db.commit()

        log.info(f"Odds sync complete for {sport_key}: {upserted} events.")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SCORE SYNC + AUTO-GRADE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _sync_scores(self):
        """Fetch scores from ESPN for all sports, update events, auto-grade bets.

        Two-pass approach:
          Pass 1 (score-driven): Fetch fresh scores from ESPN (7-day lookback),
                  update real_events, grade any pending bets for newly completed events.
          Pass 2 (bet-driven): Find pending bets whose events are already marked
                  completed in the DB but somehow weren't graded (e.g. prior DB lock,
                  crash, or ESPN returned the score but grading failed).
        """
        log.info("[REAL-SB] Syncing scores...")
        try:
            all_scores = await self.client.get_all_scores(days_from=7)
        except Exception as e:
            log.error(f"[REAL-SB] ESPN score fetch failed: {e}")
            return

        # ── Pass 1: Score-driven — update events + grade from fresh ESPN data ──
        graded_total = 0
        async with aiosqlite.connect(DB_PATH) as db:
            for sport_key, games in all_scores.items():
                for game in games:
                    event_id = game.get("event_id", "")
                    status = game.get("status", "")
                    if not event_id:
                        continue

                    home_score = game.get("home_score")
                    away_score = game.get("away_score")
                    if home_score is None or away_score is None:
                        continue

                    home_team = game.get("home_team", {}).get("display_name", "")
                    away_team = game.get("away_team", {}).get("display_name", "")
                    completed = status == "final"

                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute("""
                        UPDATE real_events SET
                            home_score = ?, away_score = ?,
                            completed = ?, locked = 1,
                            last_score_sync = ?
                        WHERE event_id = ?
                    """, (home_score, away_score, 1 if completed else 0, now, event_id))
                    await db.commit()

                    if completed:
                        count = await self._grade_event(event_id, home_team, away_team,
                                                         home_score, away_score)
                        graded_total += count

        # ── Pass 2: Bet-driven — catch orphaned pending bets with completed events ──
        orphan_graded = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await db.execute_fetchall("""
                    SELECT DISTINCT b.event_id, e.home_team, e.away_team, e.home_score, e.away_score
                    FROM real_bets b
                    JOIN real_events e ON b.event_id = e.event_id
                    WHERE b.status = 'Pending' AND e.completed = 1
                """)
                for event_id, home_team, away_team, home_score, away_score in rows:
                    if home_score is not None and away_score is not None:
                        count = await self._grade_event(
                            event_id, home_team, away_team, home_score, away_score
                        )
                        orphan_graded += count
        except Exception:
            log.exception("[REAL-SB] Pass 2 (orphan bet grading) failed")

        log.info(f"[REAL-SB] Score sync complete. Graded {graded_total} (fresh) + {orphan_graded} (orphan) bets.")

    async def _grade_event(self, event_id: str, home_team: str, away_team: str,
                           home_score: int, away_score: int) -> int:
        """Grade all pending bets for a completed event. Returns count graded."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT bet_id, discord_id, bet_type, pick, odds, line, wager_amount "
                "FROM real_bets WHERE event_id = ? AND status = 'Pending'",
                (event_id,),
            ) as cur:
                bets = await cur.fetchall()

        if not bets:
            return 0

        graded = 0
        for bet_id, uid, bet_type, pick, odds, line, wager in bets:
            result = _evaluate_bet(
                bet_type, pick, odds, line, wager,
                home_team, away_team, home_score, away_score,
            )
            # result: "Won", "Lost", "Push"
            ref_key = f"REAL_BET_{bet_id}_{result.lower()}"

            # Atomic: credit + status update in single transaction
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    # Check if already graded (idempotency)
                    row = await db.execute_fetchall(
                        "SELECT status FROM real_bets WHERE bet_id = ?", (bet_id,)
                    )
                    if not row or row[0][0] != "Pending":
                        await db.rollback()
                        continue

                    if result == "Won":
                        payout = _payout_calc(wager, odds)
                        try:
                            await flow_wallet.credit(
                                uid, payout, "REAL_BET",
                                description=f"Won: {pick} ({bet_type})",
                                reference_key=ref_key,
                            )
                        except Exception as e:
                            log.error(f"Failed to pay bet {bet_id}: {e}")
                            await db.rollback()
                            continue
                    elif result == "Push":
                        try:
                            await flow_wallet.credit(
                                uid, wager, "REAL_BET",
                                description=f"Push: {pick} ({bet_type})",
                                reference_key=ref_key,
                            )
                        except Exception as e:
                            log.error(f"Failed to refund push bet {bet_id}: {e}")
                            await db.rollback()
                            continue

                    await db.execute(
                        "UPDATE real_bets SET status = ? WHERE bet_id = ?",
                        (result, bet_id),
                    )
                    await db.commit()
                    graded += 1

                    # Post to #ledger
                    try:
                        from ledger_poster import post_bet_settlement
                        _payout = _payout_calc(wager, odds) if result == "Won" else (wager if result == "Push" else 0)
                        _bal = await flow_wallet.get_balance(uid)
                        _matchup = f"{away_team} @ {home_team}"
                        guild = self.bot.guilds[0] if self.bot.guilds else None
                        if guild:
                            await post_bet_settlement(
                                self.bot, guild.id, uid, bet_id, _matchup,
                                bet_type, pick, wager, result, _payout, _bal, source="ESPN",
                            )
                    except Exception:
                        log.warning(f"[REAL-SB] Failed to post bet {bet_id} to #ledger")

                except Exception:
                    await db.rollback()
                    raise

        # Grade any cross-sport parlay legs tied to this event
        await _grade_parlay_legs_for_event(
            event_id, home_team, away_team, home_score, away_score,
        )

        return graded

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # IMPL METHODS (for boss_cog delegation)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def status_impl(self, interaction: discord.Interaction):
        """Show sync status, API quota, pending bet count."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM real_events WHERE completed = 0"
            ) as cur:
                active_events = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT COUNT(*) FROM real_bets WHERE status = 'Pending'"
            ) as cur:
                pending_bets = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT MAX(last_odds_sync) FROM real_events"
            ) as cur:
                last_sync = (await cur.fetchone())[0] or "Never"

        embed = discord.Embed(title="\U0001f4ca Real Sportsbook Status", color=TSL_GOLD)
        embed.add_field(name="Active Events", value=str(active_events), inline=True)
        embed.add_field(name="Pending Bets", value=str(pending_bets), inline=True)
        embed.add_field(name="Last Odds Sync", value=last_sync, inline=True)
        embed.add_field(name="Source", value="ESPN (free, no quota)", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def lock_impl(self, interaction: discord.Interaction, event_id: str):
        """Manually lock an event."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE real_events SET locked = 1 WHERE event_id = ?",
                (event_id,),
            )
            await db.commit()
        await interaction.followup.send(
            f"Locked event `{event_id}`.", ephemeral=True
        )

    async def void_impl(self, interaction: discord.Interaction, event_id: str):
        """Void an event and refund all pending bets."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT bet_id, discord_id, wager_amount "
                    "FROM real_bets WHERE event_id = ? AND status = 'Pending'",
                    (event_id,),
                ) as cur:
                    bets = await cur.fetchall()

                refunded = 0
                for bet_id, uid, wager in bets:
                    ref_key = f"REAL_BET_{bet_id}_void"
                    try:
                        await flow_wallet.credit(uid, wager, "REAL_BET",
                                                  description="Voided event refund",
                                                  reference_key=ref_key,
                                                  con=db)
                    except Exception as e:
                        log.error(f"Failed to refund bet {bet_id}: {e}")
                        continue

                    await db.execute(
                        "UPDATE real_bets SET status = 'Void' WHERE bet_id = ?",
                        (bet_id,),
                    )
                    refunded += 1

                # Mark event locked + completed
                await db.execute(
                    "UPDATE real_events SET locked = 1, completed = 1 WHERE event_id = ?",
                    (event_id,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        await interaction.followup.send(
            f"Voided event `{event_id}`. Refunded **{refunded}** bets.",
            ephemeral=True,
        )

    async def grade_impl(self, interaction: discord.Interaction):
        """Force score sync + grading."""
        await self._sync_scores()
        await interaction.followup.send(
            "Score sync + grading complete.", ephemeral=True
        )

    async def sync_impl(self, interaction: discord.Interaction, sport_key: str):
        """Force odds sync for a sport."""
        if sport_key not in SUPPORTED_SPORTS:
            await interaction.followup.send(
                f"Unknown sport: `{sport_key}`. Valid: {', '.join(SUPPORTED_SPORTS.keys())}",
                ephemeral=True,
            )
            return
        await self._sync_odds(sport_key)
        await interaction.followup.send(
            f"Odds synced for `{sport_key}`.", ephemeral=True
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DISCORD UI VIEWS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EventListView(discord.ui.View):
    """Shows a list of upcoming events with a select menu to pick one."""

    def __init__(self, cog, events: list[dict], sport_key: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.events = events[:25]  # Select max 25 options
        self.sport_key = sport_key

        options = []
        for ev in self.events:
            ct = _parse_commence(ev["commence_time"])
            time_str = ct.strftime("%m/%d %I:%M %p") if ct else "TBD"
            label = f"{ev['away_team']} @ {ev['home_team']}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(
                label=label,
                value=ev["event_id"],
                description=time_str,
            ))

        select = discord.ui.Select(
            placeholder="Select a game to bet on...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    def build_embed(self) -> discord.Embed:
        sport_name = SUPPORTED_SPORTS.get(self.sport_key, self.sport_key)
        emoji = SPORT_EMOJI.get(self.sport_key, "\U0001f3c6")
        embed = discord.Embed(
            title=f"{emoji} {sport_name} — Upcoming Games",
            description=f"**{len(self.events)}** games available for betting.",
            color=TSL_GOLD,
        )
        # Show first 10 games in the embed
        lines = []
        for ev in self.events[:10]:
            ct = _parse_commence(ev["commence_time"])
            ts = f"<t:{int(ct.timestamp())}:R>" if ct else "TBD"
            lines.append(f"**{ev['away_team']}** @ **{ev['home_team']}** — {ts}")
        embed.add_field(name="Games", value="\n".join(lines) or "None", inline=False)
        return embed

    async def _on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        event_id = interaction.data["values"][0]  # type: ignore[index]

        # Find the event
        event = next((e for e in self.events if e["event_id"] == event_id), None)
        if not event:
            return await interaction.followup.send("Event not found.", ephemeral=True)

        # Fetch full event with flat odds from real_events
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM real_events WHERE event_id = ?",
                (event_id,),
            ) as cur:
                row = await cur.fetchone()

        if not row:
            return await interaction.followup.send(
                "No odds available for this game yet.", ephemeral=True
            )

        event_row = dict(row)
        # Check that at least one odds field is populated
        has_odds = any(event_row.get(k) is not None for k in
                       ("moneyline_home", "spread_home", "over_under"))
        if not has_odds:
            return await interaction.followup.send(
                "No odds available for this game yet.", ephemeral=True
            )

        view = MatchCardView(event_row)

        from sportsbook_cards import build_real_match_detail_card, card_to_file
        png = await build_real_match_detail_card(event_row, sport_key=self.sport_key)
        file = card_to_file(png, f"match_{event_id}.png")
        await interaction.followup.send(file=file, view=view, ephemeral=True)


def _short_name(full_name: str, max_len: int = 12) -> str:
    """Shorten a team name for button labels.  'Houston Cougars' → 'Houston'."""
    parts = full_name.split()
    if len(parts) == 1:
        return full_name[:max_len]
    # Keep adding words until we'd exceed max_len
    result = parts[0]
    for word in parts[1:]:
        candidate = f"{result} {word}"
        if len(candidate) > max_len:
            break
        result = candidate
    return result


class MatchCardView(discord.ui.View):
    """6-button view: one button per betting line, shown below the match detail card.

    Row 0 (home / Over):  [ML]  [Spread]  [Total]
    Row 1 (away / Under): [ML]  [Spread]  [Total]

    Reads from flat event dict (real_events row with flattened odds columns).
    """

    def __init__(self, event: dict):
        super().__init__(timeout=120)
        self.event = event

        home = event["home_team"]
        away = event["away_team"]
        short_home = _short_name(home)
        short_away = _short_name(away)

        # Build buttons from flat odds columns
        for side, team_full, short, row_idx in [
            ("home", home, short_home, 0),
            ("away", away, short_away, 1),
        ]:
            # Moneyline
            ml_key = f"moneyline_{side}"
            ml_val = event.get(ml_key)
            if ml_val is not None:
                label = f"{short} {int(ml_val):+d}"
                btn = discord.ui.Button(
                    label=label[:80], style=discord.ButtonStyle.green, row=row_idx,
                )
                btn.callback = self._make_line_cb(
                    team_full, "Moneyline", int(ml_val), None,
                )
                self.add_item(btn)

            # Spread
            spread_key = f"spread_{side}"
            spread_odds_key = f"spread_{side}_odds"
            spread_val = event.get(spread_key)
            spread_odds = event.get(spread_odds_key)
            if spread_val is not None and spread_odds is not None:
                point_str = f"{float(spread_val):+g}"
                label = f"{short} {point_str} ({int(spread_odds):+d})"
                btn = discord.ui.Button(
                    label=label[:80], style=discord.ButtonStyle.blurple, row=row_idx,
                )
                btn.callback = self._make_line_cb(
                    team_full, "Spread", int(spread_odds), float(spread_val),
                )
                self.add_item(btn)

            # Totals (Over for home row, Under for away row)
            ou_name = "Over" if side == "home" else "Under"
            ou_total = event.get("over_under")
            ou_odds_key = "over_odds" if side == "home" else "under_odds"
            ou_odds = event.get(ou_odds_key)
            if ou_total is not None and ou_odds is not None:
                label = f"{ou_name} {float(ou_total)} ({int(ou_odds):+d})"
                btn = discord.ui.Button(
                    label=label[:80], style=discord.ButtonStyle.gray, row=row_idx,
                )
                btn.callback = self._make_line_cb(
                    ou_name, ou_name, int(ou_odds), float(ou_total),
                )
                self.add_item(btn)

    def _make_line_cb(self, pick: str, bet_type: str, odds: int, line: float | None):
        """Factory that returns a callback for a specific betting line."""
        event = self.event

        async def callback(interaction: discord.Interaction):
            from flow_sportsbook import WagerPresetView, _get_balance

            balance = await flow_wallet.get_balance(interaction.user.id)
            sport_key = event.get("sport_key", "")
            source_label = SUPPORTED_SPORTS.get(
                sport_key, sport_key.split("_")[-1].upper()
            )

            async def place_bet(inter, amt):
                await inter.response.defer(ephemeral=True)
                await _place_real_bet(
                    inter, event, bet_type, pick, odds, line, amt, source_label,
                )

            view = WagerPresetView(
                pick=pick, bet_type=bet_type, odds=odds,
                display_info={
                    "matchup": f"{event['away_team']} @ {event['home_team']}",
                    "line_str": _american_to_str(odds),
                    "source_label": source_label,
                },
                user_balance=balance,
                place_bet=place_bet,
                parlay_leg={
                    "source": source_label,
                    "event_id": event["event_id"],
                    "display": f"{event['away_team']} @ {event['home_team']}",
                    "pick": pick,
                    "bet_type": bet_type,
                    "line": line or 0,
                    "odds": odds,
                },
                custom_modal_factory=lambda: CustomRealWagerModal(
                    event, bet_type, pick, odds, line,
                ),
            )
            embed = view._build_embed()
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True,
            )

        return callback


class BetTypeView(discord.ui.View):
    """Shows odds for a single event with buttons to place bets.
    (Legacy — replaced by MatchCardView, kept for backward compatibility.)
    Now reads from flat event dict instead of odds_rows.
    """

    def __init__(self, cog, event: dict):
        super().__init__(timeout=120)
        self.cog = cog
        self.event = event

    def build_embed(self) -> discord.Embed:
        ev = self.event
        ct = _parse_commence(ev["commence_time"])
        ts = f"<t:{int(ct.timestamp())}:f>" if ct else "TBD"

        embed = discord.Embed(
            title=f"{ev['away_team']} @ {ev['home_team']}",
            description=f"Kickoff: {ts}",
            color=TSL_GOLD,
        )

        # Moneyline
        ml_h, ml_a = ev.get("moneyline_home"), ev.get("moneyline_away")
        if ml_h is not None:
            embed.add_field(
                name="Moneyline",
                value=f"**{ev['home_team']}** {_american_to_str(int(ml_h))}\n"
                      f"**{ev['away_team']}** {_american_to_str(int(ml_a or 0))}",
                inline=True,
            )

        # Spread
        sp_h = ev.get("spread_home")
        if sp_h is not None:
            sp_h_odds = ev.get("spread_home_odds") or -110
            sp_a = ev.get("spread_away") or -sp_h
            sp_a_odds = ev.get("spread_away_odds") or -110
            embed.add_field(
                name="Spread",
                value=f"**{ev['home_team']}** {float(sp_h):+g} ({_american_to_str(int(sp_h_odds))})\n"
                      f"**{ev['away_team']}** {float(sp_a):+g} ({_american_to_str(int(sp_a_odds))})",
                inline=True,
            )

        # Totals
        ou = ev.get("over_under")
        if ou is not None:
            o_odds = ev.get("over_odds") or -110
            u_odds = ev.get("under_odds") or -110
            embed.add_field(
                name="Total",
                value=f"**Over** {float(ou)} ({_american_to_str(int(o_odds))})\n"
                      f"**Under** {float(ou)} ({_american_to_str(int(u_odds))})",
                inline=True,
            )

        return embed

    @discord.ui.button(label="Moneyline", style=discord.ButtonStyle.green)
    async def ml_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.event.get("moneyline_home") is None:
            return await interaction.response.send_message("No moneyline odds.", ephemeral=True)
        options = []
        for side, team_key, ml_key in [("home", "home_team", "moneyline_home"),
                                        ("away", "away_team", "moneyline_away")]:
            team = self.event[team_key]
            ml_val = self.event.get(ml_key)
            if ml_val is not None:
                label = f"{team} — {_american_to_str(int(ml_val))}"
                if len(label) > 100:
                    label = label[:97] + "..."
                options.append(discord.SelectOption(
                    label=label, value=f"{team}|{int(ml_val)}|",
                ))
        view = PickSelectView(self.cog, self.event, "Moneyline", options)
        await interaction.response.send_message("Select your **Moneyline** pick:", view=view, ephemeral=True)

    @discord.ui.button(label="Spread", style=discord.ButtonStyle.blurple)
    async def spread_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.event.get("spread_home") is None:
            return await interaction.response.send_message("No spread odds.", ephemeral=True)
        options = []
        for side in ("home", "away"):
            team = self.event[f"{side}_team"]
            sp = self.event.get(f"spread_{side}")
            sp_odds = self.event.get(f"spread_{side}_odds")
            if sp is not None and sp_odds is not None:
                label = f"{team} {float(sp):+g} ({_american_to_str(int(sp_odds))})"
                if len(label) > 100:
                    label = label[:97] + "..."
                options.append(discord.SelectOption(
                    label=label, value=f"{team}|{int(sp_odds)}|{float(sp)}",
                ))
        view = PickSelectView(self.cog, self.event, "Spread", options)
        await interaction.response.send_message("Select your **Spread** pick:", view=view, ephemeral=True)

    @discord.ui.button(label="Over/Under", style=discord.ButtonStyle.gray)
    async def totals_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ou = self.event.get("over_under")
        if ou is None:
            return await interaction.response.send_message("No totals odds.", ephemeral=True)
        options = []
        for name, odds_key in [("Over", "over_odds"), ("Under", "under_odds")]:
            odds_val = self.event.get(odds_key)
            if odds_val is not None:
                label = f"{name} {float(ou)} ({_american_to_str(int(odds_val))})"
                if len(label) > 100:
                    label = label[:97] + "..."
                options.append(discord.SelectOption(
                    label=label, value=f"{name}|{int(odds_val)}|{float(ou)}",
                ))
        view = PickSelectView(self.cog, self.event, "OU", options)
        await interaction.response.send_message("Select **Over** or **Under**:", view=view, ephemeral=True)


class PickSelectView(discord.ui.View):
    """Select menu for picking a specific outcome, then opens wager modal."""

    def __init__(self, cog, event: dict,
                 bet_type: str, options: list[discord.SelectOption]):
        super().__init__(timeout=60)
        self.cog = cog
        self.event = event
        self.bet_type = bet_type

        select = discord.ui.Select(
            placeholder="Choose your pick...",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        from flow_sportsbook import WagerPresetView, _get_balance

        raw = interaction.data["values"][0]  # type: ignore[index]
        parts = raw.split("|")
        pick = parts[0]
        odds = int(parts[1])
        line = float(parts[2]) if parts[2] and parts[2] != 'None' else None

        # Map OU bet_type
        actual_bet_type = self.bet_type
        if self.bet_type == "OU":
            actual_bet_type = pick  # "Over" or "Under"

        event = self.event
        balance = await flow_wallet.get_balance(interaction.user.id)

        # Derive source label from sport_key
        sport_key = event.get("sport_key", "")
        source_label = SUPPORTED_SPORTS.get(sport_key, sport_key.split("_")[-1].upper())

        async def place_bet(inter, amt):
            await inter.response.defer(ephemeral=True)
            await _place_real_bet(inter, event, actual_bet_type, pick, odds, line, amt, source_label)

        view = WagerPresetView(
            pick=pick, bet_type=actual_bet_type, odds=odds,
            display_info={
                "matchup": f"{event['away_team']} @ {event['home_team']}",
                "line_str": _american_to_str(odds),
                "source_label": source_label,
            },
            user_balance=balance,
            place_bet=place_bet,
            parlay_leg={
                "source": source_label,
                "event_id": event["event_id"],
                "display": f"{event['away_team']} @ {event['home_team']}",
                "pick": pick,
                "bet_type": actual_bet_type,
                "line": line or 0,
                "odds": odds,
            },
            custom_modal_factory=lambda: CustomRealWagerModal(
                event, actual_bet_type, pick, odds, line,
            ),
        )
        embed = view._build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _place_real_bet(interaction, event, bet_type, pick, odds, line, amt, source_label):
    """Place a real sport bet. Called by WagerPresetView preset callbacks.

    Handles: event re-check, commence time check, debit, DB insert, confirm card.
    Raises InsufficientFundsError if balance too low.
    """
    # Re-check event is still open
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT locked, completed, commence_time "
            "FROM real_events WHERE event_id = ?",
            (event["event_id"],),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        if not interaction.response.is_done():
            return await interaction.response.send_message("Event not found.", ephemeral=True)
        return await interaction.followup.send("Event not found.", ephemeral=True)

    locked, completed, commence_str = row
    if locked or completed:
        msg = "This game is already **locked**."
        if not interaction.response.is_done():
            return await interaction.response.send_message(msg, ephemeral=True)
        return await interaction.followup.send(msg, ephemeral=True)

    ct = _parse_commence(commence_str)
    if ct and ct <= datetime.now(timezone.utc) + timedelta(minutes=5):
        msg = "This game starts too soon to bet on."
        if not interaction.response.is_done():
            return await interaction.response.send_message(msg, ephemeral=True)
        return await interaction.followup.send(msg, ephemeral=True)

    uid = interaction.user.id

    # Debit balance (raises InsufficientFundsError)
    new_balance = await flow_wallet.debit(
        uid, amt, "REAL_BET",
        description=f"Bet: {pick} ({bet_type})",
    )

    # Insert bet
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO real_bets "
            "(discord_id, event_id, sport_key, bet_type, pick, odds, line, wager_amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, event["event_id"], event.get("sport_key", ""),
             bet_type, pick, odds, line, amt, now),
        )
        bet_id_rows = await db.execute_fetchall("SELECT last_insert_rowid()")
        bet_id = bet_id_rows[0][0]
        await db.commit()

    # Register in wager registry (was missing from the old RealBetModal)
    import wager_registry
    await wager_registry.register_wager(
        "REAL_BET", str(bet_id), uid, amt,
        label=f"{pick} {bet_type} {_american_to_str(odds)}",
        odds=odds,
    )

    profit = _profit_calc(amt, odds)
    matchup = f"{event['away_team']} @ {event['home_team']}"

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    from sportsbook_cards import build_bet_confirm_card, card_to_file
    png = await build_bet_confirm_card(
        pick=pick, bet_type=bet_type, odds=odds,
        risk=amt, to_win=profit, balance=new_balance,
        matchup=matchup, line=line, source=source_label,
    )
    file = card_to_file(png, "bet_confirm.png")
    await interaction.followup.send(file=file, ephemeral=True)


class CustomRealWagerModal(discord.ui.Modal):
    """Text-input modal for custom (non-preset) real sport wager amounts."""

    def __init__(self, event: dict,
                 bet_type: str, pick: str, odds: int, line: Optional[float]):
        super().__init__(title=f"📋 Custom Wager — {bet_type}")
        self.event = event
        self.bet_type = bet_type
        self.pick = pick
        self.odds = odds
        self.line = line

        line_str = f" ({line:+g})" if line is not None else ""
        raw_label = f"Wager | {pick}{line_str} {_american_to_str(odds)}"
        self.amount_input = discord.ui.TextInput(
            label=raw_label[:45],
            placeholder=f"Min ${MIN_BET}",
            min_length=1,
            max_length=8,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("Enter a valid number.", ephemeral=True)

        if amt < MIN_BET:
            return await interaction.response.send_message(
                f"Minimum bet is **${MIN_BET}**.", ephemeral=True)

        max_bet = await _get_max_bet()
        if amt > max_bet:
            return await interaction.response.send_message(
                f"Maximum bet is **${max_bet:,}**.", ephemeral=True)

        source_label = SUPPORTED_SPORTS.get(
            self.event.get("sport_key", ""),
            self.event.get("sport_key", "SPORTS").split("_")[-1].upper(),
        )
        try:
            await _place_real_bet(
                interaction, self.event, self.bet_type, self.pick,
                self.odds, self.line, amt, source_label,
            )
        except InsufficientFundsError:
            bal = await flow_wallet.get_balance(interaction.user.id)
            await interaction.response.send_message(
                f"Insufficient funds. Balance: **${bal:,}**.", ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SETUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def setup(bot: commands.Bot):
    await bot.add_cog(RealSportsbookCog(bot))
    print("ATLAS: Flow - Real Sportsbook loaded.")

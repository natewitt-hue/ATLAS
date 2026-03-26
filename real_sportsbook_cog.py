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
    """Parse ISO8601 commence_time string (with or without seconds)."""
    if not ct:
        return None
    try:
        return datetime.fromisoformat(ct.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
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
        import sportsbook_core
        sportsbook_core.init(self.bot)
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

        log.info(f"ESPN returned {len(games)} games for {sport_key}.")
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

        # Backfill odds via per-game endpoint for games the scoreboard returned without odds.
        # ESPN embeds odds in NFL scoreboards but not consistently for other sports (e.g. NBA).
        null_odds_ids = [
            g["event_id"] for g in games if g.get("spread", {}).get("home") is None
        ]
        if null_odds_ids:
            log.info(f"Backfilling per-game odds for {len(null_odds_ids)} {sport_key} game(s)...")
            updates = []
            for eid in null_odds_ids:
                detail = await self.client.get_game_odds(eid, league_key)
                if not detail:
                    continue
                providers = detail.get("providers", {})
                p = providers.get(1004) or (next(iter(providers.values()), None) if providers else None)
                if not p:
                    continue
                spread = p.get("spread")
                updates.append((
                    spread,
                    -spread if spread is not None else None,
                    p.get("home_moneyline"),
                    p.get("away_moneyline"),
                    p.get("over_under"),
                    eid,
                ))
            if updates:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.executemany(
                        "UPDATE real_events SET "
                        "spread_home=?, spread_away=?, moneyline_home=?, moneyline_away=?, over_under=? "
                        "WHERE event_id=?",
                        updates,
                    )
                    await db.commit()
                log.info(f"Backfilled odds for {len(updates)}/{len(null_odds_ids)} {sport_key} game(s).")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SCORE SYNC + AUTO-GRADE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _sync_scores(self):
        """Fetch scores from ESPN for all sports, update events, emit EVENT_FINALIZED."""
        import sportsbook_core
        from flow_events import flow_bus, EVENT_FINALIZED

        log.info("[REAL-SB] Syncing scores...")
        try:
            all_scores = await self.client.get_all_scores(days_from=7)
        except Exception as e:
            log.error(f"[REAL-SB] ESPN score fetch failed: {e}")
            return

        finalized_total = 0
        async with aiosqlite.connect(DB_PATH) as db:
            for sport_key, games in all_scores.items():
                for game in games:
                    espn_event_id = game.get("event_id", "")
                    status = game.get("status", "")
                    if not espn_event_id:
                        continue

                    home_score = game.get("home_score")
                    away_score = game.get("away_score")
                    if home_score is None or away_score is None:
                        continue

                    home_team = game.get("home_team", {}).get("display_name", "")
                    away_team = game.get("away_team", {}).get("display_name", "")
                    commence_ts = game.get("event_date", "")
                    completed = status == "final"

                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute("""
                        UPDATE real_events SET
                            home_score = ?, away_score = ?,
                            completed = ?, locked = 1,
                            last_score_sync = ?
                        WHERE event_id = ?
                    """, (home_score, away_score, 1 if completed else 0, now, espn_event_id))

                    if completed:
                        event_id = f"real:{sport_key}:{espn_event_id}"
                        try:
                            await sportsbook_core.write_event(
                                event_id, "REAL", home_team, away_team, commence_ts
                            )
                            await sportsbook_core.finalize_event(
                                event_id, home_score, away_score
                            )
                            await flow_bus.emit(
                                EVENT_FINALIZED,
                                {"event_id": event_id, "source": "REAL"},
                            )
                            finalized_total += 1
                        except Exception:
                            log.exception(
                                f"[REAL-SB] Failed to finalize event {event_id}"
                            )

                await db.commit()

        log.info(f"[REAL-SB] Score sync complete. Finalized {finalized_total} events.")

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







async def _place_real_bet(interaction, event, bet_type, pick, odds, line, amt, source_label):
    """Place a real sport bet.

    Returns (new_balance, profit, matchup) on success so the caller can render
    the confirmation inline. Returns None if the bet was blocked (error already sent).
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

    async def _send_err(msg: str):
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    if not row:
        await _send_err("Event not found.")
        return None

    locked, completed, commence_str = row
    if locked or completed:
        await _send_err("This game is already **locked**.")
        return None

    ct = _parse_commence(commence_str)
    if ct and ct <= datetime.now(timezone.utc) + timedelta(minutes=5):
        await _send_err("This game starts too soon to bet on.")
        return None

    uid = interaction.user.id

    # Debit balance (raises InsufficientFundsError)
    new_balance = await flow_wallet.debit(
        uid, amt, "REAL_BET",
        description=f"Bet: {pick} ({bet_type})",
    )

    # Write bet to sportsbook_core (flow.db) and legacy real_bets for admin reads
    import sportsbook_core
    sport_key = event.get("sport_key", "")
    espn_event_id = event["event_id"]
    core_event_id = f"real:{sport_key}:{espn_event_id}"
    commence_ts = event.get("commence_time", "")

    try:
        await sportsbook_core.write_event(
            event_id=core_event_id,
            source="REAL",
            home=event.get("home_team", ""),
            away=event.get("away_team", ""),
            commence_ts=commence_ts,
        )
        bet_id = await sportsbook_core.write_bet(
            discord_id=uid,
            event_id=core_event_id,
            bet_type=bet_type,
            pick=pick,
            line=line,
            odds=odds,
            wager=amt,
        )
    except Exception:
        log.exception(f"[REAL-SB] write_event/write_bet failed for uid={uid} event={core_event_id} — refunding")
        try:
            await flow_wallet.credit(
                uid, amt, "REAL_BET",
                description="Refund: bet record write failed",
                reference_key=f"REAL_BET_WRITE_FAILED_{uid}_{espn_event_id}",
            )
        except Exception:
            log.exception(f"[REAL-SB] CRITICAL: refund also failed for uid={uid} — manual intervention required")
        await _send_err("⚠️ Bet placement failed — your funds have been returned. Please try again.")
        return None

    # Register in wager registry (was missing from the old RealBetModal)
    import wager_registry
    await wager_registry.register_wager(
        "REAL_BET", str(bet_id), uid, amt,
        label=f"{pick} {bet_type} {_american_to_str(odds)}",
        odds=odds,
    )

    profit = _profit_calc(amt, odds)
    matchup = f"{event['away_team']} @ {event['home_team']}"
    return new_balance, profit, matchup


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
        await interaction.response.defer(ephemeral=True)
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.followup.send("Enter a valid number.", ephemeral=True)

        if amt < MIN_BET:
            return await interaction.followup.send(
                f"Minimum bet is **${MIN_BET}**.", ephemeral=True)

        max_bet = await _get_max_bet()
        if amt > max_bet:
            return await interaction.followup.send(
                f"Maximum bet is **${max_bet:,}**.", ephemeral=True)

        source_label = SUPPORTED_SPORTS.get(
            self.event.get("sport_key", ""),
            self.event.get("sport_key", "SPORTS").split("_")[-1].upper(),
        )
        try:
            result = await _place_real_bet(
                interaction, self.event, self.bet_type, self.pick,
                self.odds, self.line, amt, source_label,
            )
        except InsufficientFundsError:
            bal = await flow_wallet.get_balance(interaction.user.id)
            await interaction.followup.send(
                f"Insufficient funds. Balance: **${bal:,}**.", ephemeral=True)
            return

        if result is not None:
            new_balance, profit, matchup = result
            from sportsbook_cards import build_bet_confirm_card, card_to_file
            png = await build_bet_confirm_card(
                pick=self.pick, bet_type=self.bet_type, odds=self.odds,
                risk=amt, to_win=profit, balance=new_balance,
                matchup=matchup, line=self.line, source=source_label,
            )
            file = card_to_file(png, "bet_confirm.png")
            await interaction.followup.send(file=file, ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SETUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def setup(bot: commands.Bot):
    await bot.add_cog(RealSportsbookCog(bot))
    print("ATLAS: Flow - Real Sportsbook loaded.")

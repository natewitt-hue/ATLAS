"""
real_sportsbook_cog.py — ATLAS Flow: Real Sports Sportsbook
=============================================================
Bet on real sports with TSL Bucks using live odds from TheRundown API.

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
import os
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

import flow_wallet
from flow_wallet import DB_PATH, InsufficientFundsError
from odds_api_client import OddsAPIClient, SUPPORTED_SPORTS

log = logging.getLogger("real_sportsbook")

# ── Config ────────────────────────────────────────────────────────────────────

MIN_BET = 50
DEFAULT_MAX_BET = 5000
TSL_GOLD = 0xC8A951

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

def _american_to_str(odds: int) -> str:
    """Format American odds as string (+150 / -110)."""
    return f"+{odds}" if odds > 0 else str(odds)


def _payout_calc(wager: int, odds: int) -> int:
    """Calculate total payout (wager + profit) from American odds."""
    if odds > 0:
        return wager + int(wager * odds / 100)
    else:
        return wager + int(wager * 100 / abs(odds))


def _profit_calc(wager: int, odds: int) -> int:
    """Calculate profit only from American odds."""
    return _payout_calc(wager, odds) - wager


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


# ── Cog ───────────────────────────────────────────────────────────────────────

class RealSportsbookCog(commands.Cog, name="RealSportsbookCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = OddsAPIClient()
        self._ready = False

    async def _setup_tables(self):
        """Create real sportsbook tables if they don't exist."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS real_events (
                    event_id       TEXT PRIMARY KEY,
                    sport_key      TEXT NOT NULL,
                    sport_title    TEXT NOT NULL,
                    home_team      TEXT NOT NULL,
                    away_team      TEXT NOT NULL,
                    commence_time  TEXT,
                    home_score     INTEGER,
                    away_score     INTEGER,
                    locked         INTEGER DEFAULT 0,
                    completed      INTEGER DEFAULT 0,
                    last_odds_sync  TEXT,
                    last_score_sync TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS real_odds (
                    event_id      TEXT NOT NULL,
                    bookmaker     TEXT NOT NULL,
                    market        TEXT NOT NULL,
                    outcome_name  TEXT NOT NULL,
                    price         INTEGER,
                    point         REAL,
                    last_updated  TEXT,
                    UNIQUE(event_id, bookmaker, market, outcome_name)
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
        log.info("Real sportsbook tables ready.")

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

    @tasks.loop(minutes=15)
    async def sync_scores_task(self):
        """Fetch scores every 15 minutes, auto-grade completed bets."""
        await asyncio.sleep(random.uniform(5, 15))
        await self._sync_scores()

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
        """Fetch odds from API and upsert into real_events + real_odds."""
        log.info(f"Syncing odds for {sport_key}...")
        events = await self.client.fetch_odds(sport_key)
        if not events:
            log.warning(f"No events returned for {sport_key}.")
            return

        now = datetime.now(timezone.utc).isoformat()
        sport_title = SUPPORTED_SPORTS.get(sport_key, sport_key)
        upserted = 0

        async with aiosqlite.connect(DB_PATH) as db:
            for ev in events:
                event_id = ev.get("id", "")
                home = ev.get("home_team", "")
                away = ev.get("away_team", "")
                commence = ev.get("commence_time", "")
                if not event_id or not home or not away:
                    continue

                # Upsert event
                await db.execute("""
                    INSERT INTO real_events
                        (event_id, sport_key, sport_title, home_team, away_team,
                         commence_time, last_odds_sync)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        home_team      = excluded.home_team,
                        away_team      = excluded.away_team,
                        commence_time  = excluded.commence_time,
                        last_odds_sync = excluded.last_odds_sync
                """, (event_id, sport_key, sport_title, home, away, commence, now))

                # Upsert odds from each bookmaker
                for bk in ev.get("bookmakers", []):
                    bk_key = bk.get("key", "")
                    for market in bk.get("markets", []):
                        mkt_key = market.get("key", "")  # h2h, spreads, totals
                        for outcome in market.get("outcomes", []):
                            name = outcome.get("name", "")
                            price = outcome.get("price", 0)
                            point = outcome.get("point")
                            await db.execute("""
                                INSERT INTO real_odds
                                    (event_id, bookmaker, market, outcome_name,
                                     price, point, last_updated)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(event_id, bookmaker, market, outcome_name)
                                DO UPDATE SET
                                    price        = excluded.price,
                                    point        = excluded.point,
                                    last_updated = excluded.last_updated
                            """, (event_id, bk_key, mkt_key, name, price, point, now))
                upserted += 1

            await db.commit()

        log.info(f"Odds sync complete for {sport_key}: {upserted} events.")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SCORE SYNC + AUTO-GRADE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _sync_scores(self):
        """Fetch scores for all sports, update events, auto-grade bets."""
        log.info("Syncing real sportsbook scores...")
        all_scores = await self.client.fetch_all_scores(days_from=3)

        graded_total = 0
        # Hoist DB connection outside the loop to avoid reconnecting per-event
        async with aiosqlite.connect(DB_PATH) as db:
            for sport_key, events in all_scores.items():
                for ev in events:
                    event_id = ev.get("id", "")
                    completed = ev.get("completed", False)
                    scores = ev.get("scores")
                    if not event_id or not scores:
                        continue

                    home_score = None
                    away_score = None
                    home_team = ev.get("home_team", "")
                    away_team = ev.get("away_team", "")

                    for s in scores:
                        if s.get("name") == home_team:
                            try:
                                home_score = int(s.get("score", 0))
                            except (ValueError, TypeError):
                                pass
                        elif s.get("name") == away_team:
                            try:
                                away_score = int(s.get("score", 0))
                            except (ValueError, TypeError):
                                pass

                    if home_score is None or away_score is None:
                        continue

                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute("""
                        UPDATE real_events SET
                            home_score = ?, away_score = ?,
                            completed = ?, locked = 1,
                            last_score_sync = ?
                        WHERE event_id = ?
                    """, (home_score, away_score, 1 if completed else 0, now, event_id))
                    await db.commit()

                    # Only grade when game is fully completed
                    if completed:
                        count = await self._grade_event(event_id, home_team, away_team,
                                                         home_score, away_score)
                        graded_total += count

        log.info(f"Score sync complete. Graded {graded_total} bets.")

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
            result = self._evaluate_bet(
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
                except Exception:
                    await db.rollback()
                    raise

        return graded

    def _evaluate_bet(self, bet_type: str, pick: str, odds: int, line: float,
                      wager: int, home_team: str, away_team: str,
                      home_score: int, away_score: int) -> str:
        """
        Evaluate a single bet. Returns 'Won', 'Lost', or 'Push'.

        bet_type: 'Moneyline', 'Spread', 'Over', 'Under'
        """
        total = home_score + away_score

        if bet_type == "Moneyline":
            if home_score == away_score:
                return "Push"
            if pick == home_team:
                return "Won" if home_score > away_score else "Lost"
            else:
                return "Won" if away_score > home_score else "Lost"

        elif bet_type == "Spread":
            # line is from the perspective of the picked team
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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # IMPL METHODS (for commish_cog delegation)
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

        remaining = self.client.requests_remaining
        used = self.client.requests_used
        if remaining is not None:
            embed.add_field(
                name="API Quota",
                value=f"{remaining} remaining / {used} used",
                inline=True,
            )
            if self.client.emergency_mode:
                embed.add_field(name="Mode", value="EMERGENCY (odds paused)", inline=True)

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
        event_id = interaction.data["values"][0]

        # Find the event
        event = next((e for e in self.events if e["event_id"] == event_id), None)
        if not event:
            return await interaction.followup.send("Event not found.", ephemeral=True)

        # Fetch odds for this event
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT market, outcome_name, price, point "
                "FROM real_odds WHERE event_id = ? "
                "ORDER BY market, outcome_name",
                (event_id,),
            ) as cur:
                odds_rows = [dict(row) for row in await cur.fetchall()]

        if not odds_rows:
            return await interaction.followup.send(
                "No odds available for this game yet.", ephemeral=True
            )

        view = BetTypeView(self.cog, event, odds_rows)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class BetTypeView(discord.ui.View):
    """Shows odds for a single event with buttons to place bets."""

    def __init__(self, cog, event: dict, odds_rows: list[dict]):
        super().__init__(timeout=120)
        self.cog = cog
        self.event = event
        self.odds_rows = odds_rows

        # Group odds by market
        self.markets: dict[str, list[dict]] = {}
        for row in odds_rows:
            mkt = row["market"]
            self.markets.setdefault(mkt, []).append(row)

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
        h2h = self.markets.get("h2h", [])
        if h2h:
            ml_lines = []
            for o in h2h:
                ml_lines.append(f"**{o['outcome_name']}** {_american_to_str(o['price'])}")
            embed.add_field(name="Moneyline", value="\n".join(ml_lines), inline=True)

        # Spread
        spreads = self.markets.get("spreads", [])
        if spreads:
            sp_lines = []
            for o in spreads:
                point_str = f"{o['point']:+g}" if o['point'] is not None else ""
                sp_lines.append(
                    f"**{o['outcome_name']}** {point_str} ({_american_to_str(o['price'])})"
                )
            embed.add_field(name="Spread", value="\n".join(sp_lines), inline=True)

        # Totals
        totals = self.markets.get("totals", [])
        if totals:
            t_lines = []
            for o in totals:
                point_str = f"{o['point']}" if o['point'] is not None else ""
                t_lines.append(
                    f"**{o['outcome_name']}** {point_str} ({_american_to_str(o['price'])})"
                )
            embed.add_field(name="Total", value="\n".join(t_lines), inline=True)

        return embed

    @discord.ui.button(label="Moneyline", style=discord.ButtonStyle.green)
    async def ml_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        h2h = self.markets.get("h2h", [])
        if not h2h:
            return await interaction.response.send_message("No moneyline odds.", ephemeral=True)
        await self._show_pick_select(interaction, "Moneyline", h2h)

    @discord.ui.button(label="Spread", style=discord.ButtonStyle.blurple)
    async def spread_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        spreads = self.markets.get("spreads", [])
        if not spreads:
            return await interaction.response.send_message("No spread odds.", ephemeral=True)
        await self._show_pick_select(interaction, "Spread", spreads)

    @discord.ui.button(label="Over/Under", style=discord.ButtonStyle.gray)
    async def totals_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        totals = self.markets.get("totals", [])
        if not totals:
            return await interaction.response.send_message("No totals odds.", ephemeral=True)
        await self._show_ou_select(interaction, totals)

    async def _show_pick_select(self, interaction: discord.Interaction,
                                 bet_type: str, outcomes: list[dict]):
        """Show select menu for picking a team (ML or Spread)."""
        options = []
        for o in outcomes:
            point_str = f" ({o['point']:+g})" if o.get('point') is not None else ""
            label = f"{o['outcome_name']}{point_str} — {_american_to_str(o['price'])}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(
                label=label,
                value=f"{o['outcome_name']}|{o['price']}|{o.get('point') or ''}",
            ))

        view = PickSelectView(self.cog, self.event, bet_type, options)
        await interaction.response.send_message(
            f"Select your **{bet_type}** pick:",
            view=view,
            ephemeral=True,
        )

    async def _show_ou_select(self, interaction: discord.Interaction, outcomes: list[dict]):
        """Show select for Over or Under."""
        options = []
        for o in outcomes:
            name = o["outcome_name"]  # "Over" or "Under"
            bt = "Over" if name == "Over" else "Under"
            point_str = f" {o['point']}" if o.get('point') is not None else ""
            label = f"{name}{point_str} — {_american_to_str(o['price'])}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(
                label=label,
                value=f"{name}|{o['price']}|{o.get('point') or ''}",
            ))

        view = PickSelectView(self.cog, self.event, "OU", options)
        await interaction.response.send_message(
            "Select **Over** or **Under**:",
            view=view,
            ephemeral=True,
        )


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
        raw = interaction.data["values"][0]
        parts = raw.split("|")
        pick = parts[0]
        odds = int(parts[1])
        line = float(parts[2]) if parts[2] and parts[2] != 'None' else None

        # Map OU bet_type
        actual_bet_type = self.bet_type
        if self.bet_type == "OU":
            actual_bet_type = pick  # "Over" or "Under"

        modal = RealBetModal(
            self.cog, self.event, actual_bet_type, pick, odds, line
        )
        await interaction.response.send_modal(modal)


class RealBetModal(discord.ui.Modal):
    """Modal to enter wager amount for a real sports bet."""

    def __init__(self, cog, event: dict,
                 bet_type: str, pick: str, odds: int, line: Optional[float]):
        super().__init__(title=f"Bet Slip — {bet_type}")
        self.cog = cog
        self.event = event
        self.bet_type = bet_type
        self.pick = pick
        self.odds = odds
        self.line = line

        line_str = f" ({line:+g})" if line is not None else ""
        self.amount_input = discord.ui.TextInput(
            label=f"Wager | {pick}{line_str} {_american_to_str(odds)}",
            placeholder=f"Min ${MIN_BET}",
            min_length=1,
            max_length=8,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate amount
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message(
                "Enter a valid number.", ephemeral=True
            )

        if amt < MIN_BET:
            return await interaction.response.send_message(
                f"Minimum bet is **${MIN_BET}**.", ephemeral=True
            )

        max_bet = await _get_max_bet()
        if amt > max_bet:
            return await interaction.response.send_message(
                f"Maximum bet is **${max_bet:,}**.", ephemeral=True
            )

        # Re-check event is still open
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT locked, completed, commence_time "
                "FROM real_events WHERE event_id = ?",
                (self.event["event_id"],),
            ) as cur:
                row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "Event not found.", ephemeral=True
            )

        locked, completed, commence_str = row
        if locked or completed:
            return await interaction.response.send_message(
                "This game is already **locked**.", ephemeral=True
            )

        ct = _parse_commence(commence_str)
        if ct and ct <= datetime.now(timezone.utc) + timedelta(minutes=5):
            return await interaction.response.send_message(
                "This game starts too soon to bet on.", ephemeral=True
            )

        # Debit balance
        uid = interaction.user.id
        try:
            new_balance = await flow_wallet.debit(
                uid, amt, "REAL_BET",
                description=f"Bet: {self.pick} ({self.bet_type})",
            )
        except InsufficientFundsError:
            bal = await flow_wallet.get_balance(uid)
            return await interaction.response.send_message(
                f"Insufficient funds. Balance: **${bal:,}**.", ephemeral=True
            )

        # Insert bet
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO real_bets "
                "(discord_id, event_id, sport_key, bet_type, pick, odds, line, wager_amount, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, self.event["event_id"], self.event["sport_key"],
                 self.bet_type, self.pick, self.odds, self.line, amt, now),
            )
            await db.commit()

        profit = _profit_calc(amt, self.odds)
        matchup = f"{self.event['away_team']} @ {self.event['home_team']}"

        embed = discord.Embed(title="Bet Confirmed", color=TSL_GOLD)
        embed.add_field(name="Game", value=matchup, inline=False)
        embed.add_field(name="Pick", value=f"**{self.pick}**", inline=True)
        embed.add_field(name="Type", value=self.bet_type, inline=True)
        embed.add_field(name="Odds", value=_american_to_str(self.odds), inline=True)
        if self.line is not None:
            embed.add_field(name="Line", value=f"{self.line:+g}", inline=True)
        embed.add_field(name="Risk", value=f"**${amt:,}**", inline=True)
        embed.add_field(name="To Win", value=f"**${profit:,}**", inline=True)
        embed.add_field(name="Balance", value=f"${new_balance:,}", inline=True)
        embed.set_footer(text="ATLAS Real Sportsbook")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SETUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def setup(bot: commands.Bot):
    await bot.add_cog(RealSportsbookCog(bot))
    print("ATLAS: Flow - Real Sportsbook loaded.")

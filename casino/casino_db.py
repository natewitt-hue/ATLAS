"""
casino_db.py -- TSL Casino Database Layer
---------------------------------------------------------------------------
All async DB operations for the casino.  Uses aiosqlite with BEGIN IMMEDIATE
transactions to prevent race conditions when multiple users gamble at once.

Shares flow_economy.db -- reads/writes the same users_table.balance column.
Balance operations delegate to flow_wallet for unified transaction logging.
All casino tables are created here on first startup via setup_casino_db().
---------------------------------------------------------------------------
"""

import aiosqlite
import hashlib
import logging
import os
import random
import secrets
import string
from datetime import datetime, timezone, date, timedelta

import flow_wallet

log = logging.getLogger("casino.db")

# -- Shared DB path (unified economy DB) --------------------------------------
DB_PATH = flow_wallet.DB_PATH

CASINO_MAX_BET     = 100
CASINO_DAILY_MIN   = 25
CASINO_DAILY_MAX   = 150
CASINO_MAX_PAYOUT  = 10_000_000  # sanity cap — matches sportsbook MAX_PAYOUT
STARTING_BALANCE  = flow_wallet.STARTING_BALANCE

# -- Jackpot configuration ----------------------------------------------------
JACKPOT_CONTRIBUTION_RATE = 0.01       # 1% of every wager
JACKPOT_SPLIT = {"mini": 0.50, "major": 0.30, "grand": 0.20}
JACKPOT_SEEDS = {"mini": 100, "major": 500, "grand": 2000}
# Base trigger odds per $100 wagered
JACKPOT_BASE_ODDS = {"mini": 500, "major": 5_000, "grand": 50_000}

# -- Streak configuration -----------------------------------------------------
STREAK_BONUSES = {
    3:  {"pct": 0.05, "label": "Hot Hand",     "jackpot_mult": 1},
    5:  {"pct": 0.10, "label": "On Fire",      "jackpot_mult": 1},
    7:  {"pct": 0.15, "label": "Untouchable",  "jackpot_mult": 2},
    10: {"pct": 0.20, "label": "LEGENDARY",    "jackpot_mult": 3},
}
COLD_STREAK_THRESHOLDS = {
    5:  {"type": "next_win_boost", "value": 1.25},
    8:  {"type": "free_credit",    "value": 50},
    10: {"type": "loss_refund_pct","value": 0.25},
}

# -- Progressive bet tiers -----------------------------------------------------
BET_TIERS = [
    (100_000, 1000, "Diamond"),
    (25_000,   500, "Gold"),
    (5_000,    250, "Silver"),
    (0,        100, "Bronze"),
]


# ═════════════════════════════════════════════════════════════════════════════
#  TABLE SETUP
# ═════════════════════════════════════════════════════════════════════════════

async def setup_casino_db() -> None:
    """
    Create all casino tables if they don't exist.
    Safe to call on every startup — uses IF NOT EXISTS throughout.
    Does NOT touch any existing sportsbook tables.
    """
    async with aiosqlite.connect(DB_PATH) as db:

        # ── Enable WAL mode (shared with sportsbook.py for lock-free reads) ─
        await db.execute("PRAGMA journal_mode=WAL")

        # ── Casino session log (one row per completed game) ────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_sessions (
                session_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id     INTEGER NOT NULL,
                game_type      TEXT    NOT NULL,
                wager          INTEGER NOT NULL,
                outcome        TEXT    NOT NULL,
                payout         INTEGER NOT NULL,
                multiplier     REAL    DEFAULT 1.0,
                channel_id     INTEGER,
                played_at      TEXT    NOT NULL,
                correlation_id TEXT
            )
        """)

        # ── House bank — running P&L per game type ─────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_house_bank (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                game_type   TEXT    NOT NULL,
                delta       INTEGER NOT NULL,
                session_id  INTEGER,
                recorded_at TEXT    NOT NULL
            )
        """)

        # ── Crash rounds — shared round state ─────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crash_rounds (
                round_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                crash_point REAL    NOT NULL,
                seed        TEXT    NOT NULL,
                status      TEXT    DEFAULT 'open',
                started_at  TEXT,
                crashed_at  TEXT,
                created_at  TEXT    NOT NULL
            )
        """)

        # ── Crash bets within a shared round ──────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crash_bets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id     INTEGER NOT NULL,
                discord_id   INTEGER NOT NULL,
                wager        INTEGER NOT NULL,
                cashout_mult REAL    DEFAULT NULL,
                payout       INTEGER DEFAULT 0,
                status       TEXT    DEFAULT 'active'
            )
        """)

        # ── Daily scratch card claims ──────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_scratches (
                discord_id  INTEGER NOT NULL,
                last_claim  TEXT    NOT NULL,
                PRIMARY KEY (discord_id)
            )
        """)

        # ── PvP coin flip challenges ───────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coinflip_challenges (
                challenge_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                challenger_id INTEGER NOT NULL,
                opponent_id   INTEGER NOT NULL,
                wager         INTEGER NOT NULL,
                channel_id    INTEGER NOT NULL,
                status        TEXT    DEFAULT 'pending',
                winner_id     INTEGER DEFAULT NULL,
                created_at    TEXT    NOT NULL,
                resolved_at   TEXT    DEFAULT NULL
            )
        """)

        # ── Progressive jackpot pools ────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_jackpot (
                tier        TEXT    PRIMARY KEY,
                pool        INTEGER NOT NULL DEFAULT 0,
                seed        INTEGER NOT NULL DEFAULT 0,
                last_winner INTEGER DEFAULT NULL,
                last_amount INTEGER DEFAULT NULL,
                last_won_at TEXT    DEFAULT NULL,
                total_paid  INTEGER NOT NULL DEFAULT 0,
                total_hits  INTEGER NOT NULL DEFAULT 0
            )
        """)
        for tier, seed_val in JACKPOT_SEEDS.items():
            await db.execute(
                "INSERT OR IGNORE INTO casino_jackpot (tier, pool, seed) VALUES (?,?,?)",
                (tier, seed_val, seed_val),
            )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_jackpot_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tier       TEXT    NOT NULL,
                discord_id INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                game_type  TEXT    NOT NULL,
                won_at     TEXT    NOT NULL
            )
        """)

        # ── Player streaks ───────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_streaks (
                discord_id  INTEGER PRIMARY KEY,
                streak_type TEXT    NOT NULL DEFAULT 'none',
                streak_len  INTEGER NOT NULL DEFAULT 0,
                max_streak  INTEGER NOT NULL DEFAULT 0,
                streak_date TEXT    NOT NULL DEFAULT '',
                updated_at  TEXT    NOT NULL DEFAULT ''
            )
        """)

        # ── Achievements ─────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS casino_achievements (
                discord_id  INTEGER NOT NULL,
                achievement TEXT    NOT NULL,
                unlocked_at TEXT    NOT NULL,
                PRIMARY KEY (discord_id, achievement)
            )
        """)

        # ── Daily scratch streak columns (add via ALTER if missing) ──────
        try:
            await db.execute(
                "ALTER TABLE daily_scratches ADD COLUMN login_streak INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists
        try:
            await db.execute(
                "ALTER TABLE daily_scratches ADD COLUMN last_streak_date TEXT DEFAULT ''"
            )
        except Exception:
            pass

        # ── casino_sessions correlation_id (link session ↔ debit txn) ────
        try:
            await db.execute(
                "ALTER TABLE casino_sessions ADD COLUMN correlation_id TEXT"
            )
        except Exception:
            pass  # column already exists
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cs_discord_corr "
            "ON casino_sessions (discord_id, correlation_id)"
        )

        # -- Casino settings (max bet overrides, channel IDs, etc.) --------
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sportsbook_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            INSERT OR IGNORE INTO sportsbook_settings (key, value)
            VALUES
                ('casino_open',             '1'),
                ('casino_blackjack_open',   '1'),
                ('casino_crash_open',       '1'),
                ('casino_slots_open',       '1'),
                ('casino_coinflip_open',    '1'),
                ('casino_max_bet',          '100'),
                ('casino_daily_min',        '25'),
                ('casino_daily_max',        '150'),
                ('casino_hub_channel',      ''),
                ('casino_blackjack_channel',''),
                ('casino_crash_channel',    ''),
                ('casino_slots_channel',    ''),
                ('casino_coinflip_channel', ''),
                ('casino_daily_cap',        '5000')
        """)

        await db.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  ORPHAN WAGER RECONCILIATION
# ═════════════════════════════════════════════════════════════════════════════

async def reconcile_orphaned_wagers(max_age_minutes: int = 30) -> list[dict]:
    """
    Find casino wager debits with no matching casino_sessions entry and refund them.
    Called at startup to recover from bot crashes mid-game.

    An orphan is a transaction where:
    - source='CASINO', description='casino wager', amount < 0
    - created_at is older than max_age_minutes ago
    - discord_id + abs(amount) has no matching casino_sessions entry after the txn time

    Returns list of refunded wagers for logging.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    refunded = []

    async with aiosqlite.connect(DB_PATH) as db:
        # Find all casino wager debits older than cutoff
        async with db.execute("""
            SELECT t.txn_id, t.discord_id, ABS(t.amount) as wager, t.created_at
            FROM transactions t
            WHERE t.source = 'CASINO'
              AND t.description = 'casino wager'
              AND t.amount < 0
              AND t.created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM casino_sessions s
                  WHERE s.discord_id = t.discord_id
                    AND s.wager = ABS(t.amount)
                    AND s.played_at >= t.created_at
              )
              AND NOT EXISTS (
                  SELECT 1 FROM transactions t2
                  WHERE t2.discord_id = t.discord_id
                    AND t2.source = 'CASINO'
                    AND t2.description = 'orphan refund'
                    AND t2.reference_key = 'ORPHAN_' || t.txn_id
              )
        """, (cutoff,)) as cur:
            orphans = await cur.fetchall()

    for txn_id, discord_id, wager, created_at in orphans:
        try:
            await flow_wallet.credit(
                discord_id, wager, "CASINO",
                description="orphan refund",
                reference_key=f"ORPHAN_{txn_id}",
            )
            refunded.append({
                "txn_id": txn_id,
                "discord_id": discord_id,
                "wager": wager,
                "created_at": created_at,
            })
            log.warning(
                f"Refunded orphaned wager: ${wager} to user {discord_id} "
                f"(txn {txn_id} from {created_at})"
            )
        except Exception as e:
            log.error(f"Failed to refund orphan txn {txn_id}: {e}")

    if refunded:
        log.info(f"Reconciliation complete: refunded {len(refunded)} orphaned wagers")
    return refunded


# ═════════════════════════════════════════════════════════════════════════════
#  SETTINGS HELPERS  (async versions for casino use)
# ═════════════════════════════════════════════════════════════════════════════

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM sportsbook_settings WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sportsbook_settings (key,value) VALUES (?,?)",
            (key, value)
        )
        await db.commit()


async def is_casino_open(game: str | None = None) -> bool:
    """Check if the whole casino is open, or a specific game."""
    if not await get_setting("casino_open", "1") == "1":
        return False
    if game:
        return await get_setting(f"casino_{game}_open", "1") == "1"
    return True


async def get_channel_id(game: str) -> int | None:
    """Return the registered channel ID for a game, or None if not set."""
    val = await get_setting(f"casino_{game}_channel", "")
    try:
        return int(val) if val else None
    except ValueError:
        return None


async def get_max_bet(discord_id: int | None = None) -> int:
    """
    Return max bet.  If discord_id is given, apply progressive tier limits.
    """
    base = CASINO_MAX_BET
    val = await get_setting("casino_max_bet", "100")
    try:
        base = int(val)
    except ValueError:
        pass

    if discord_id is None:
        return base

    tier = await get_player_tier(discord_id)
    return tier["max_bet"]


async def get_player_tier(discord_id: int) -> dict:
    """Return player's bet tier based on lifetime wagered volume."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(wager),0) FROM casino_sessions WHERE discord_id=?",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()
    lifetime = row[0] if row else 0
    for threshold, max_bet, name in BET_TIERS:
        if lifetime >= threshold:
            return {"name": name, "max_bet": max_bet, "lifetime": lifetime, "threshold": threshold}
    return {"name": "Bronze", "max_bet": 100, "lifetime": lifetime, "threshold": 0}


# ═════════════════════════════════════════════════════════════════════════════
#  BALANCE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def get_balance(discord_id: int) -> int:
    return await flow_wallet.get_balance(discord_id)


# ═════════════════════════════════════════════════════════════════════════════
#  JACKPOT SYSTEM
# ═════════════════════════════════════════════════════════════════════════════

async def get_jackpot_pools() -> dict[str, dict]:
    """Return current pool amounts for all tiers."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM casino_jackpot ORDER BY tier") as cur:
            rows = await cur.fetchall()
    cols = ["tier", "pool", "seed", "last_winner", "last_amount", "last_won_at", "total_paid", "total_hits"]
    return {r[0]: dict(zip(cols, r)) for r in rows}


async def _contribute_and_check_jackpot(
    discord_id: int, wager: int, game_type: str, streak_len: int, con, session_id: int
) -> dict | None:
    """
    Contribute wager % to jackpot pools and roll for jackpot win.
    Must be called inside an existing BEGIN IMMEDIATE transaction (con).
    Returns jackpot info dict if won, else None.
    """
    total_contrib = max(1, int(wager * JACKPOT_CONTRIBUTION_RATE))

    for tier, split_pct in JACKPOT_SPLIT.items():
        contrib = max(1, int(total_contrib * split_pct))
        await con.execute(
            "UPDATE casino_jackpot SET pool = pool + ? WHERE tier = ?",
            (contrib, tier),
        )

    # Roll for jackpot — higher wager = proportionally higher chance
    wager_factor = wager / 100.0

    # Streak multiplier for jackpot odds
    jp_mult = 1
    for threshold in sorted(STREAK_BONUSES.keys(), reverse=True):
        if streak_len >= threshold:
            jp_mult = STREAK_BONUSES[threshold]["jackpot_mult"]
            break

    # Check jackpot boost event
    boost = 1.0
    async with con.execute(
        "SELECT value FROM sportsbook_settings WHERE key='casino_jackpot_boost'"
    ) as cur:
        row = await cur.fetchone()
    if row:
        try:
            parts = row[0].split(",")  # "multiplier,expires_iso"
            if len(parts) == 2 and datetime.fromisoformat(parts[1]) > datetime.now(timezone.utc):
                boost = float(parts[0])
        except (ValueError, IndexError):
            pass

    roll = random.random()

    # Check tiers from rarest to most common
    for tier in ("grand", "major", "mini"):
        base_odds = JACKPOT_BASE_ODDS[tier]
        threshold = (wager_factor * jp_mult * boost) / base_odds
        if roll < threshold:
            return await _award_jackpot(tier, discord_id, game_type, con, session_id)

    return None


async def _award_jackpot(tier: str, discord_id: int, game_type: str, con, session_id: int) -> dict:
    """Award jackpot pool to the winner. Returns info dict."""
    now = datetime.now(timezone.utc).isoformat()
    async with con.execute(
        "SELECT pool, seed FROM casino_jackpot WHERE tier=?", (tier,)
    ) as cur:
        row = await cur.fetchone()

    amount = row[0] if row else 0
    seed_val = row[1] if row else JACKPOT_SEEDS.get(tier, 100)

    if amount < 1:
        return None

    # Credit winner
    ref_key = f"JACKPOT_{tier}_{discord_id}_{now}"
    await flow_wallet.credit(
        discord_id, amount, "CASINO",
        description=f"JACKPOT {tier.upper()} win!",
        reference_key=ref_key,
        subsystem="CASINO",
        subsystem_id=str(session_id),
        con=con,
    )

    # Reset pool to seed, update stats
    await con.execute("""
        UPDATE casino_jackpot
        SET pool = ?, last_winner = ?, last_amount = ?, last_won_at = ?,
            total_paid = total_paid + ?, total_hits = total_hits + 1
        WHERE tier = ?
    """, (seed_val, discord_id, amount, now, amount, tier))

    # Record jackpot payout in house bank (money leaving house to player)
    await con.execute(
        "INSERT INTO casino_house_bank (game_type, delta, session_id, recorded_at) VALUES (?,?,?,?)",
        ("jackpot_payout", -amount, session_id, now),
    )

    # Log
    await con.execute("""
        INSERT INTO casino_jackpot_log (tier, discord_id, amount, game_type, won_at)
        VALUES (?,?,?,?,?)
    """, (tier, discord_id, amount, game_type, now))

    log.info("JACKPOT %s won by %s: $%d (%s)", tier.upper(), discord_id, amount, game_type)
    return {"tier": tier, "amount": amount, "discord_id": discord_id, "game_type": game_type}


async def seed_jackpot(tier: str, amount: int) -> None:
    """Admin: add funds to a jackpot pool."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE casino_jackpot SET pool = pool + ? WHERE tier = ?",
            (amount, tier),
        )
        await db.commit()


async def backfill_jackpot_tags() -> int:
    """
    One-time migration: tag historical jackpot credit transactions with
    subsystem='CASINO' and subsystem_id='JP_<log_id>'.
    Idempotent — only affects rows where subsystem IS NULL.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE transactions SET subsystem='CASINO', subsystem_id='JP_' || (
                SELECT jl.id FROM casino_jackpot_log jl
                WHERE jl.discord_id = transactions.discord_id
                  AND jl.amount = transactions.amount
                  AND ABS(julianday(jl.won_at) - julianday(transactions.created_at)) < 0.001
                ORDER BY ABS(julianday(jl.won_at) - julianday(transactions.created_at))
                LIMIT 1
            )
            WHERE transactions.description LIKE 'JACKPOT%'
              AND transactions.subsystem IS NULL
        """)
        count = cursor.rowcount
        await db.commit()
    return count


# ═════════════════════════════════════════════════════════════════════════════
#  STREAK SYSTEM ("Momentum")
# ═════════════════════════════════════════════════════════════════════════════

async def get_streak(discord_id: int) -> dict:
    """Return current streak info for a player."""
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT streak_type, streak_len, max_streak, streak_date FROM casino_streaks WHERE discord_id=?",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or row[3] != today:
        return {"type": "none", "len": 0, "max": row[2] if row else 0, "date": today}
    return {"type": row[0], "len": row[1], "max": row[2], "date": row[3]}


async def _update_streak(discord_id: int, outcome: str, con) -> dict:
    """
    Update streak state within an existing transaction.
    Returns updated streak info dict.
    """
    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    async with con.execute(
        "SELECT streak_type, streak_len, max_streak, streak_date FROM casino_streaks WHERE discord_id=?",
        (discord_id,),
    ) as cur:
        row = await cur.fetchone()

    if outcome == "push":
        if not row:
            return {"type": "none", "len": 0, "max": 0, "date": today}
        return {"type": row[0], "len": row[1], "max": row[2], "date": row[3]}

    # Reset streak if from a different day
    prev_type = row[0] if row and row[3] == today else "none"
    prev_len  = row[1] if row and row[3] == today else 0
    prev_max  = row[2] if row else 0

    if outcome == "win":
        new_type = "win"
        new_len  = (prev_len + 1) if prev_type == "win" else 1
    else:
        new_type = "loss"
        new_len  = (prev_len + 1) if prev_type == "loss" else 1

    new_max = max(prev_max, new_len) if new_type == "win" else prev_max

    await con.execute("""
        INSERT INTO casino_streaks (discord_id, streak_type, streak_len, max_streak, streak_date, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(discord_id) DO UPDATE SET
            streak_type=excluded.streak_type,
            streak_len=excluded.streak_len,
            max_streak=MAX(casino_streaks.max_streak, excluded.max_streak),
            streak_date=excluded.streak_date,
            updated_at=excluded.updated_at
    """, (discord_id, new_type, new_len, new_max, today, now))

    return {"type": new_type, "len": new_len, "max": new_max, "date": today}


def get_streak_bonus(streak_info: dict) -> dict | None:
    """Return the active streak bonus for a given streak, or None."""
    if streak_info["type"] != "win":
        return None
    for threshold in sorted(STREAK_BONUSES.keys(), reverse=True):
        if streak_info["len"] >= threshold:
            return {**STREAK_BONUSES[threshold], "threshold": threshold}
    return None


def get_cold_streak_mercy(streak_info: dict) -> dict | None:
    """Return the active cold streak mercy mechanic, or None."""
    if streak_info["type"] != "loss":
        return None
    for threshold in sorted(COLD_STREAK_THRESHOLDS.keys(), reverse=True):
        if streak_info["len"] >= threshold:
            return {**COLD_STREAK_THRESHOLDS[threshold], "threshold": threshold}
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  ACHIEVEMENTS
# ═════════════════════════════════════════════════════════════════════════════

# Achievement definitions: {id: {name, description, check_fn_name}}
ACHIEVEMENTS = {
    "first_timer":    {"name": "First Timer",    "desc": "Play 1 casino game",          "icon": "bronze_chip"},
    "regular":        {"name": "Regular",        "desc": "Play 50 games",               "icon": "silver_chip"},
    "high_roller":    {"name": "High Roller",    "desc": "Play 500 games",              "icon": "gold_chip"},
    "whale":          {"name": "Whale",          "desc": "Play 1,000 games",            "icon": "plat_chip"},
    "lucky_7":        {"name": "Lucky 7",        "desc": "7 win streak",                "icon": "seven_stars"},
    "perfect_hand":   {"name": "Perfect Hand",   "desc": "Blackjack with A+K suited",   "icon": "royal_crown"},
    "rocketman":      {"name": "Rocketman",      "desc": "Cash out at 10x+ in crash",   "icon": "rocket"},
    "moon_shot":      {"name": "Moon Shot",      "desc": "Cash out at 50x+ in crash",   "icon": "moon"},
    "nerves_of_steel":{"name": "Nerves of Steel","desc": "Last Man Standing 3 times",   "icon": "shield"},
    "all_rounder":    {"name": "All-Rounder",    "desc": "Play all 4 game types",       "icon": "four_leaf"},
    "jackpot_club":   {"name": "Jackpot Club",   "desc": "Hit any jackpot tier",        "icon": "diamond"},
    "grand_slam":     {"name": "Grand Slam",     "desc": "Hit all 3 jackpot tiers",     "icon": "triple_diamond"},
    "comeback_king":  {"name": "Comeback King",  "desc": "Win after 8+ loss streak",    "icon": "phoenix"},
    "dedicated":      {"name": "Dedicated",      "desc": "14-day daily scratch streak", "icon": "calendar_star"},
    "iron_will":      {"name": "Iron Will",      "desc": "30-day daily scratch streak", "icon": "iron_cross"},
    "big_spender":    {"name": "Big Spender",    "desc": "$10K lifetime wagered",       "icon": "money_bag"},
    "high_society":   {"name": "High Society",   "desc": "$50K lifetime wagered",       "icon": "crown"},
    "legend":         {"name": "Legend",          "desc": "$100K lifetime wagered",      "icon": "trophy"},
    "challenger":     {"name": "Challenger",      "desc": "Win 10 PvP coin flips",      "icon": "crossed_swords"},
    "crowd_player":   {"name": "Crowd Player",   "desc": "Play 20 crash rounds (3+ players)", "icon": "stadium"},
}


async def get_player_achievements(discord_id: int) -> set[str]:
    """Return set of unlocked achievement IDs for a player."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT achievement FROM casino_achievements WHERE discord_id=?",
            (discord_id,),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def check_achievements(
    discord_id: int,
    game_type: str,
    outcome: str,
    multiplier: float,
    streak_info: dict,
    jackpot_result: dict | None = None,
) -> list[dict]:
    """
    Check and award any newly unlocked achievements.
    Returns list of newly unlocked achievement dicts.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = await get_player_achievements(discord_id)
    newly_unlocked = []

    async with aiosqlite.connect(DB_PATH) as db:
        # Milestone checks
        async with db.execute(
            "SELECT COUNT(*) FROM casino_sessions WHERE discord_id=?", (discord_id,)
        ) as cur:
            total_games = (await cur.fetchone())[0]

        milestones = [("first_timer", 1), ("regular", 50), ("high_roller", 500), ("whale", 1000)]
        for ach_id, threshold in milestones:
            if ach_id not in existing and total_games >= threshold:
                newly_unlocked.append(ach_id)

        # Economy checks
        async with db.execute(
            "SELECT COALESCE(SUM(wager),0) FROM casino_sessions WHERE discord_id=?", (discord_id,)
        ) as cur:
            lifetime_wagered = (await cur.fetchone())[0]

        econ = [("big_spender", 10_000), ("high_society", 50_000), ("legend", 100_000)]
        for ach_id, threshold in econ:
            if ach_id not in existing and lifetime_wagered >= threshold:
                newly_unlocked.append(ach_id)

        # All-Rounder (played all 4 game types)
        if "all_rounder" not in existing:
            async with db.execute(
                "SELECT DISTINCT game_type FROM casino_sessions WHERE discord_id=? AND game_type IN ('blackjack','slots','crash','coinflip')",
                (discord_id,),
            ) as cur:
                types = await cur.fetchall()
            if len(types) >= 4:
                newly_unlocked.append("all_rounder")

        # Lucky 7 (7 win streak)
        if "lucky_7" not in existing and streak_info["type"] == "win" and streak_info["len"] >= 7:
            newly_unlocked.append("lucky_7")

        # Comeback King (win after 8+ loss streak)
        if "comeback_king" not in existing and outcome == "win":
            # Check if previous streak was 8+ losses (streak just reset to win 1)
            if streak_info["type"] == "win" and streak_info["len"] == 1:
                async with db.execute(
                    "SELECT streak_len FROM casino_streaks WHERE discord_id=?", (discord_id,)
                ) as cur:
                    pass  # We can't check previous streak after reset
                # Alternative: check last 9 sessions (8 losses + this win)
                async with db.execute(
                    "SELECT outcome FROM casino_sessions WHERE discord_id=? ORDER BY session_id DESC LIMIT 9",
                    (discord_id,),
                ) as cur:
                    recent = [r[0] for r in await cur.fetchall()]
                if len(recent) >= 9 and recent[0] == "win" and all(o == "loss" for o in recent[1:9]):
                    newly_unlocked.append("comeback_king")

        # Crash achievements
        if "rocketman" not in existing and game_type == "crash" and outcome == "win" and multiplier >= 10.0:
            newly_unlocked.append("rocketman")
        if "moon_shot" not in existing and game_type == "crash" and outcome == "win" and multiplier >= 50.0:
            newly_unlocked.append("moon_shot")

        # PvP coinflip challenger
        if "challenger" not in existing:
            async with db.execute(
                "SELECT COUNT(*) FROM casino_sessions WHERE discord_id=? AND game_type='coinflip_pvp' AND outcome='win'",
                (discord_id,),
            ) as cur:
                pvp_wins = (await cur.fetchone())[0]
            if pvp_wins >= 10:
                newly_unlocked.append("challenger")

        # Jackpot achievements
        if jackpot_result:
            if "jackpot_club" not in existing:
                newly_unlocked.append("jackpot_club")
            if "grand_slam" not in existing:
                async with db.execute(
                    "SELECT DISTINCT tier FROM casino_jackpot_log WHERE discord_id=?",
                    (discord_id,),
                ) as cur:
                    tiers_hit = {r[0] for r in await cur.fetchall()}
                tiers_hit.add(jackpot_result["tier"])
                if tiers_hit >= {"mini", "major", "grand"}:
                    newly_unlocked.append("grand_slam")

        # Persist new achievements
        for ach_id in newly_unlocked:
            await db.execute(
                "INSERT OR IGNORE INTO casino_achievements (discord_id, achievement, unlocked_at) VALUES (?,?,?)",
                (discord_id, ach_id, now),
            )
        if newly_unlocked:
            await db.commit()

    return [{"id": a, **ACHIEVEMENTS[a]} for a in newly_unlocked if a in ACHIEVEMENTS]


# ═════════════════════════════════════════════════════════════════════════════
#  CORE WAGER PROCESSOR  — race-condition safe
# ═════════════════════════════════════════════════════════════════════════════

InsufficientFundsError = flow_wallet.InsufficientFundsError

class CasinoClosedError(Exception):
    pass


async def process_wager(
    discord_id:  int,
    wager:       int,
    game_type:   str,
    outcome:     str,       # 'win' | 'loss' | 'push'
    payout:      int,       # total bucks returned to player (0 if loss)
    multiplier:  float = 1.0,
    channel_id:  int | None = None,
    correlation_id: str | None = None,
) -> dict:
    """
    Atomically process a casino wager result.

    Flow:
      1. BEGIN IMMEDIATE — acquires write lock
      2. Update streak, apply hot/cold streak bonuses to payout
      3. Credit payout to balance
      4. Log session + house bank delta
      5. Contribute to jackpot pools + roll for jackpot
      6. COMMIT

    Returns dict with session_id, new_balance, txn_id, streak_info,
    streak_bonus, jackpot_result, cold_mercy.
    """
    now = datetime.now(timezone.utc).isoformat()
    streak_info = {"type": "none", "len": 0, "max": 0, "date": ""}
    streak_bonus_info = None
    jackpot_result = None
    cold_mercy = None
    bonus_amount = 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")

        try:
            # 1. Update streak
            streak_info = await _update_streak(discord_id, outcome, db)

            # 2. Apply streak bonuses
            if outcome == "win" and payout > 0:
                bonus = get_streak_bonus(streak_info)
                if bonus and bonus["pct"] > 0:
                    bonus_amount = int(payout * bonus["pct"])
                    # Fund bonus from mini jackpot pool
                    async with db.execute(
                        "SELECT pool FROM casino_jackpot WHERE tier='mini'"
                    ) as cur:
                        jp_row = await cur.fetchone()
                    if jp_row and jp_row[0] >= bonus_amount:
                        payout += bonus_amount
                        await db.execute(
                            "UPDATE casino_jackpot SET pool = pool - ? WHERE tier='mini'",
                            (bonus_amount,),
                        )
                        streak_bonus_info = {"amount": bonus_amount, **bonus}
                    else:
                        bonus_amount = 0

            # 3. Cold streak mercy
            if outcome == "loss":
                mercy = get_cold_streak_mercy(streak_info)
                if mercy:
                    if mercy["type"] == "loss_refund_pct" and streak_info["len"] >= 10:
                        refund = int(wager * mercy["value"])
                        if refund > 0:
                            payout += refund
                            cold_mercy = {"type": "loss_refund", "amount": refund}

            # 4. Apply payout sanity cap
            if payout > CASINO_MAX_PAYOUT:
                log.error(f"[CASINO] Insane payout ${payout:,.2f} for {game_type} — capping to ${CASINO_MAX_PAYOUT:,.2f}")
                payout = CASINO_MAX_PAYOUT

            # 5. Log session BEFORE credit so we have session_id for tagging
            async with db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier,
                     channel_id, played_at, correlation_id)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (discord_id, game_type, wager, outcome, payout, multiplier,
                  channel_id, now, correlation_id)) as cur:
                session_id = cur.lastrowid

            sid = str(session_id)

            # 6. Credit payout via flow_wallet (tagged with session_id at creation)
            if payout > 0:
                ref_key = f"CASINO_{game_type}_{discord_id}_{now}"
                new_balance = await flow_wallet.credit(
                    discord_id, payout, "CASINO",
                    description=f"{game_type} {outcome}",
                    reference_key=ref_key,
                    subsystem="CASINO",
                    subsystem_id=sid,
                    con=db,
                )
            else:
                new_balance = await flow_wallet.get_balance(discord_id, con=db)

            # 7. Cold streak free credit (at 8+ losses, once per streak)
            if outcome == "loss" and streak_info["type"] == "loss" and streak_info["len"] >= 8:
                mercy = get_cold_streak_mercy(streak_info)
                if mercy and mercy["type"] == "free_credit":
                    credit_amt = int(mercy["value"])
                    ref_key2 = f"MERCY_{discord_id}_{now}"
                    new_balance = await flow_wallet.credit(
                        discord_id, credit_amt, "CASINO",
                        description="cold streak mercy credit",
                        reference_key=ref_key2,
                        subsystem="CASINO",
                        subsystem_id=sid,
                        con=db,
                    )
                    cold_mercy = {"type": "free_credit", "amount": credit_amt}

            # 8. Backlink debit txn to this session + wager registry
            if correlation_id:
                await db.execute(
                    "UPDATE transactions SET subsystem_id=? "
                    "WHERE discord_id=? AND subsystem='CASINO' AND subsystem_id=?",
                    (sid, discord_id, correlation_id),
                )
                # Backlink wager registry: correlation_id → session_id
                import wager_registry
                await db.execute(
                    "UPDATE wagers SET subsystem_id=?, label=? "
                    "WHERE subsystem='CASINO' AND subsystem_id=?",
                    (sid, f"Casino {game_type}", correlation_id),
                )

            # 8b. Settle wager in unified registry
            import wager_registry
            _casino_status = "won" if outcome == "win" else ("push" if outcome == "push" else "lost")
            await wager_registry.settle_wager("CASINO", sid, _casino_status, payout - wager, con=db)

            # 9. Get txn_id
            txn_id = None
            async with db.execute(
                "SELECT txn_id FROM transactions "
                "WHERE discord_id=? ORDER BY txn_id DESC LIMIT 1",
                (discord_id,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    txn_id = row[0]

            # 10. Log house bank (delta uses original wager vs final payout)
            house_delta = wager - payout
            await db.execute("""
                INSERT INTO casino_house_bank (game_type, delta, session_id, recorded_at)
                VALUES (?,?,?,?)
            """, (game_type, house_delta, session_id, now))

            # 11. Jackpot contribution + roll
            if wager > 0 and session_id is not None:
                _sid: int = session_id
                jackpot_result = await _contribute_and_check_jackpot(
                    discord_id, wager, game_type, streak_info.get("len", 0), db, _sid
                )
                if jackpot_result:
                    new_balance = await flow_wallet.get_balance(discord_id, con=db)

            await db.commit()

        except Exception:
            await db.rollback()
            raise

    return {
        "session_id":     session_id,
        "new_balance":    new_balance,
        "txn_id":         txn_id,
        "streak_info":    streak_info,
        "streak_bonus":   streak_bonus_info,
        "jackpot_result": jackpot_result,
        "cold_mercy":     cold_mercy,
    }


async def deduct_wager(discord_id: int, wager: int,
                       correlation_id: str | None = None) -> int:
    """
    Deduct wager from balance at bet placement time.
    Returns new balance.
    Raises InsufficientFundsError if balance is too low.
    Raises ValueError if wager exceeds tier limit.
    correlation_id: optional UUID fragment to link this debit to its
    future casino_sessions row (prevents race conditions on backlink).
    """
    balance = await flow_wallet.get_balance(discord_id)
    max_bet = CASINO_MAX_BET
    for threshold, limit, _ in BET_TIERS:
        if balance >= threshold:
            max_bet = limit
            break
    if wager > max_bet:
        raise ValueError(f"Wager ${wager:,} exceeds your tier limit of ${max_bet:,}")
    async with flow_wallet.get_user_lock(discord_id):
        new_bal = await flow_wallet.debit(
            discord_id, wager, "CASINO",
            description="casino wager",
            subsystem="CASINO",
            subsystem_id=correlation_id,
        )
        # Register wager in unified registry (non-atomic — see GAP 5 spec)
        if correlation_id:
            import wager_registry
            await wager_registry.register_wager(
                "CASINO", correlation_id, discord_id, wager, label="Casino",
            )
        return new_bal


async def refund_wager(discord_id: int, amount: int,
                       correlation_id: str | None = None) -> int:
    """Refund a wager (e.g. declined PvP challenge, crash round void). Returns new balance."""
    new_bal = await flow_wallet.credit(
        discord_id, amount, "CASINO",
        description="casino refund",
        subsystem="CASINO", subsystem_id=correlation_id,
    )
    if correlation_id:
        import wager_registry
        await wager_registry.settle_wager("CASINO", correlation_id, "voided", 0)
    return new_bal


# ═════════════════════════════════════════════════════════════════════════════
#  HOUSE BANK REPORTING
# ═════════════════════════════════════════════════════════════════════════════

async def get_house_report() -> dict:
    """Return P&L breakdown by game type plus totals and rolling edge stats."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT game_type, SUM(delta) as pl, COUNT(*) as hands
            FROM casino_house_bank
            GROUP BY game_type
            ORDER BY pl DESC
        """) as cur:
            rows = await cur.fetchall()

        async with db.execute("""
            SELECT COUNT(DISTINCT discord_id), COUNT(*), SUM(wager)
            FROM casino_sessions
        """) as cur:
            totals = await cur.fetchone()

        # Rolling 7-day edge per game
        async with db.execute("""
            SELECT cs.game_type,
                   SUM(cs.wager) as wagered,
                   SUM(cs.payout) as paid,
                   COUNT(*) as hands
            FROM casino_sessions cs
            WHERE cs.played_at >= date('now', '-7 days')
            GROUP BY cs.game_type
        """) as cur:
            rolling_rows = await cur.fetchall()

    rolling_7d = {}
    for r in rolling_rows:
        wagered = r[1] or 0
        paid = r[2] or 0
        edge = ((wagered - paid) / wagered * 100) if wagered > 0 else 0
        rolling_7d[r[0]] = {"wagered": wagered, "paid": paid, "edge_pct": round(edge, 2), "hands": r[3]}

    return {
        "by_game": [{"game": r[0], "pl": r[1], "hands": r[2]} for r in rows],
        "total_pl": sum(r[1] for r in rows),
        "unique_players": totals[0] if totals else 0,
        "total_hands":    totals[1] if totals else 0,
        "total_wagered":  totals[2] if totals else 0,
        "rolling_7d":     rolling_7d,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  DAILY SCRATCH CARD
# ═════════════════════════════════════════════════════════════════════════════

async def can_claim_scratch(discord_id: int) -> bool:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_claim FROM daily_scratches WHERE discord_id=?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
    return row is None or row[0] != today


async def get_scratch_streak(discord_id: int) -> int:
    """Return current daily scratch login streak."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT login_streak, last_streak_date FROM daily_scratches WHERE discord_id=?",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return 0
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today = date.today().isoformat()
    if row[1] in (yesterday, today):
        return row[0]
    return 0  # streak broken


async def claim_scratch(discord_id: int, reward: int | None = None) -> dict | None:
    """
    Credit a scratch card reward to the user's balance.
    Pass the pre-computed reward from the UI tiles so what the player
    sees matches what they receive.  Falls back to a random roll if
    reward is not provided (backwards compat).
    Returns dict with amount, streak, bonus_pct — or None if already claimed.
    """
    if not await can_claim_scratch(discord_id):
        return None

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Fallback: roll if caller didn't pass a pre-computed reward
    if reward is None:
        reward_pool = [
            (25,  40),   # (amount, weight)
            (50,  30),
            (75,  15),
            (100, 10),
            (150,  5),
        ]
        amounts  = [r[0] for r in reward_pool]
        weights  = [r[1] for r in reward_pool]
        reward   = random.choices(amounts, weights=weights, k=1)[0]

    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Re-check inside transaction to prevent TOCTOU double-claim
            async with db.execute(
                "SELECT last_claim, login_streak, last_streak_date FROM daily_scratches WHERE discord_id=?",
                (discord_id,),
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] == today:
                await db.rollback()
                return None

            # Calculate streak
            if row and row[2] == yesterday:
                new_streak = (row[1] or 0) + 1
            else:
                new_streak = 1

            # Apply streak bonus (+10% per day, capped at +100%)
            bonus_pct = min(new_streak, 10) * 0.10
            bonus_amount = int(reward * bonus_pct)
            total_reward = reward + bonus_amount

            # Mark claimed with streak
            await db.execute("""
                INSERT INTO daily_scratches (discord_id, last_claim, login_streak, last_streak_date)
                VALUES (?,?,?,?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    last_claim=excluded.last_claim,
                    login_streak=excluded.login_streak,
                    last_streak_date=excluded.last_streak_date
            """, (discord_id, today, new_streak, today))

            # Credit balance
            ref_key = f"SCRATCH_{discord_id}_{today}"
            await flow_wallet.credit(
                discord_id, total_reward, "CASINO",
                description=f"daily scratch (day {new_streak} streak)",
                reference_key=ref_key,
                con=db,
            )

            # Log as casino session
            await db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, played_at)
                VALUES (?,?,?,?,?,?,?)
            """, (discord_id, "scratch", 0, "win", total_reward, 1.0, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return {"amount": total_reward, "base": reward, "streak": new_streak, "bonus_pct": bonus_pct}


# ═════════════════════════════════════════════════════════════════════════════
#  CRASH ROUND MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

def _generate_crash_point() -> tuple[float, str]:
    """
    Generate a provably fair crash point.

    Distribution (tuned for house edge):
      ~35% crash before 2x
      ~25% between 2x – 5x
      ~25% between 5x – 15x
      ~15% beyond 15x
      Hard cap at 100x

    Uses a seeded hash so the crash point can be revealed after the round
    as proof it wasn't manipulated.
    """
    seed = secrets.token_urlsafe(16)
    h    = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    # Map hash to 0.0–1.0
    p    = (h % 10_000_000) / 10_000_000

    # Inverse CDF for exponential-ish distribution
    # P(crash < x) = 1 - 1/x  (classic crash curve)
    # With a house edge adjustment: use 0.97 so expected value < 1
    house_edge = 0.97
    if p < (1 - house_edge):
        crash_point = 1.0   # instant crash (very rare but possible)
    else:
        crash_point = house_edge / (1.0 - p)

    crash_point = min(round(crash_point, 2), 100.0)
    return crash_point, seed


async def create_crash_round(channel_id: int) -> int:
    """Create a new crash round. Returns round_id."""
    crash_point, seed = _generate_crash_point()
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO crash_rounds (channel_id, crash_point, seed, status, created_at)
            VALUES (?,?,?,'open',?)
        """, (channel_id, crash_point, seed, now)) as cur:
            round_id = cur.lastrowid
        await db.commit()
    return round_id


async def get_crash_round(round_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM crash_rounds WHERE round_id=?", (round_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def add_crash_bet(round_id: int, discord_id: int, wager: int) -> int:
    """Add a player bet to a crash round. Returns crash_bet id."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO crash_bets (round_id, discord_id, wager)
            VALUES (?,?,?)
        """, (round_id, discord_id, wager)) as cur:
            bet_id = cur.lastrowid
        await db.commit()
    return bet_id


async def cashout_crash_bet(bet_id: int, discord_id: int, multiplier: float) -> int:
    """
    Cash out a crash bet at the given multiplier.
    Marks the bet as cashed in the crash_bets table.
    Returns payout amount.  Balance credit is handled by process_wager().
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT wager, status FROM crash_bets WHERE id=? AND discord_id=?",
            (bet_id, discord_id)
        ) as cur:
            row = await cur.fetchone()

        if not row or row[1] != "active":
            return 0

        wager  = row[0]
        payout = int(wager * multiplier)

        await db.execute(
            "UPDATE crash_bets SET cashout_mult=?, payout=?, status='cashed' WHERE id=?",
            (multiplier, payout, bet_id)
        )
        await db.commit()

    return payout


async def resolve_crash_round(round_id: int) -> list[dict]:
    """
    Mark a crash round as crashed. Returns list of all bets with outcomes.
    Players who didn't cash out are already at $0 payout (wager deducted at bet time).
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Mark uncashed bets as lost
        await db.execute(
            "UPDATE crash_bets SET status='lost' WHERE round_id=? AND status='active'",
            (round_id,)
        )
        await db.execute(
            "UPDATE crash_rounds SET status='crashed', crashed_at=? WHERE round_id=?",
            (now, round_id)
        )

        async with db.execute(
            "SELECT * FROM crash_bets WHERE round_id=?", (round_id,)
        ) as cur:
            rows = await cur.fetchall()

        await db.commit()

    return [
        {
            "id":           r[0], "round_id":     r[1],
            "discord_id":   r[2], "wager":        r[3],
            "cashout_mult": r[4], "payout":       r[5],
            "status":       r[6],
        }
        for r in rows
    ]


# ═════════════════════════════════════════════════════════════════════════════
#  COIN FLIP CHALLENGES
# ═════════════════════════════════════════════════════════════════════════════

async def create_challenge(
    challenger_id: int,
    opponent_id:   int,
    wager:         int,
    channel_id:    int,
) -> int:
    """Create a PvP coin flip challenge. Returns challenge_id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            INSERT INTO coinflip_challenges
                (challenger_id, opponent_id, wager, channel_id, created_at)
            VALUES (?,?,?,?,?)
        """, (challenger_id, opponent_id, wager, channel_id, now)) as cur:
            cid = cur.lastrowid
        await db.commit()
    return cid


async def get_challenge(challenge_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM coinflip_challenges WHERE challenge_id=?", (challenge_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    keys = ["challenge_id","challenger_id","opponent_id","wager",
            "channel_id","status","winner_id","created_at","resolved_at"]
    return dict(zip(keys, row))


async def resolve_challenge(challenge_id: int, winner_id: int, loser_id: int, wager: int) -> int:
    """
    Resolve a PvP challenge. Winner gets 1.9x (slight house edge).
    Loser's wager was already deducted. Returns winner payout.
    """
    # Verify the challenge wager matches the passed wager (symmetric check)
    challenge = await get_challenge(challenge_id)
    if challenge and challenge["wager"] != wager:
        raise ValueError(
            f"Wager mismatch: challenge has {challenge['wager']} but resolve called with {wager}"
        )
    payout = int(wager * 1.9)
    now    = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Credit winner via flow_wallet (passes con -- no commit)
            ref_key = f"COINFLIP_WIN_{challenge_id}"
            await flow_wallet.credit(
                winner_id, payout, "CASINO",
                description=f"coinflip PvP win vs {loser_id}",
                reference_key=ref_key,
                con=db,
            )

            # Update challenge record
            await db.execute("""
                UPDATE coinflip_challenges
                SET status='completed', winner_id=?, resolved_at=?
                WHERE challenge_id=?
            """, (winner_id, now, challenge_id))

            # Log sessions for both players
            await db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, played_at)
                VALUES (?,?,?,?,?,?,?)
            """, (winner_id, "coinflip_pvp", wager, "win", payout, 1.9, now))
            await db.execute("""
                INSERT INTO casino_sessions
                    (discord_id, game_type, wager, outcome, payout, multiplier, played_at)
                VALUES (?,?,?,?,?,?,?)
            """, (loser_id, "coinflip_pvp", wager, "loss", 0, 0.0, now))

            # House keeps the 0.1x edge (2 wagers - 1.9x payout)
            house_delta = wager * 2 - payout
            await db.execute("""
                INSERT INTO casino_house_bank (game_type, delta, recorded_at)
                VALUES (?,?,?)
            """, ("coinflip_pvp", house_delta, now))

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return payout


async def decline_challenge(challenge_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE coinflip_challenges
            SET status='declined', resolved_at=?
            WHERE challenge_id=?
        """, (now, challenge_id))
        await db.commit()



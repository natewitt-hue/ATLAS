# sportsbook_core.py
import os, asyncio, json, logging
from datetime import datetime, timezone
import aiosqlite
from discord.ext import tasks
import flow_wallet
import wager_registry
from odds_utils import payout_calc as _payout_calc

log = logging.getLogger("sportsbook_core")

_DIR = os.path.dirname(os.path.abspath(__file__))


# ─── Grading ────────────────────────────────────────────────────────────────────

def grade_bet(bet_row: dict, event_row: dict) -> str:
    """
    Pure grading function — no DB access, no side effects.
    Returns: 'Won' | 'Lost' | 'Push' | 'Cancelled'

    bet_row keys:  bet_type, pick, line, odds, wager_amount
    event_row keys: home_participant, away_participant,
                    home_score, away_score, result_payload (JSON str), status

    Grading rules:
    - Cancelled event → 'Cancelled'
    - Moneyline: pick the winner by score; tie → 'Push'
    - Spread: covered = pick_score + line - opponent_score; >0 Won, ==0 Push, <0 Lost
              (line is from picked team's perspective; negative means favored)
    - Over: total > line → Won; total == line → Push; total < line → Lost
    - Under: total < line → Won; total == line → Push; total > line → Lost
    - Prediction: parse result_payload JSON, compare pick to payload['resolved_side'];
                  'VOID' result → Push
    """
    # TODO: implement — this function will be filled in by the user
    raise NotImplementedError("grade_bet() not yet implemented")
OLD_SB_DB = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
FLOW_DB   = os.path.join(_DIR, "flow.db")

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('TSL','REAL','POLY')),
    status TEXT NOT NULL DEFAULT 'scheduled'
           CHECK(status IN ('scheduled','live','final','cancelled')),
    home_participant TEXT, away_participant TEXT,
    home_score REAL, away_score REAL,
    result_payload TEXT,
    commence_ts TEXT, finalized_ts TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    bet_type TEXT NOT NULL CHECK(bet_type IN ('Moneyline','Spread','Over','Under','Prediction')),
    pick TEXT NOT NULL, line REAL, odds INTEGER NOT NULL, wager_amount INTEGER NOT NULL,
    status TEXT DEFAULT 'Pending'
           CHECK(status IN ('Pending','Won','Lost','Push','Cancelled','Error')),
    parlay_id TEXT REFERENCES parlays(parlay_id),
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS parlays (
    parlay_id TEXT PRIMARY KEY, discord_id INTEGER NOT NULL,
    combined_odds INTEGER NOT NULL, wager_amount INTEGER NOT NULL,
    status TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Won','Lost','Push','Cancelled')),
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS parlay_legs (
    leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id TEXT NOT NULL REFERENCES parlays(parlay_id),
    bet_id INTEGER NOT NULL REFERENCES bets(bet_id),
    leg_index INTEGER NOT NULL, UNIQUE(parlay_id, leg_index)
);

CREATE TABLE IF NOT EXISTS event_locks (event_id TEXT PRIMARY KEY REFERENCES events(event_id), locked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS event_line_overrides (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    home_spread REAL, away_spread REAL, home_ml INTEGER, away_ml INTEGER,
    ou_line REAL, set_by TEXT, set_at TEXT
);
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '7');

CREATE INDEX IF NOT EXISTS idx_events_source_status ON events(source, status);
CREATE INDEX IF NOT EXISTS idx_bets_event_status    ON bets(event_id, status);
CREATE INDEX IF NOT EXISTS idx_bets_user            ON bets(discord_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bets_parlay          ON bets(parlay_id) WHERE parlay_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_parlays_user_status  ON parlays(discord_id, status);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay   ON parlay_legs(parlay_id);
"""

async def setup_db() -> None:
    """Create flow.db schema. Called at bot startup before migration."""
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
    log.info("[CORE] flow.db schema ready")


async def run_migration_v7() -> None:
    """
    One-time migration guard. Skips if schema_version >= 7.
    Steps: refund Pending bets, archive old tables, create new schema.
    """
    # Guard: check if migration already ran
    try:
        async with aiosqlite.connect(FLOW_DB) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'")
            row = await cur.fetchone()
            if row and int(row[0]) >= 7:
                log.info("[CORE] Migration v7 already applied — skipping")
                return
    except Exception:
        pass  # FLOW_DB may not exist yet

    log.warning("[CORE] Running migration v7 — refunding Pending bets and archiving tables")

    REFUND_SOURCES = [
        ("bets_table",           "bet_id", "wager_amount"),
        ("real_bets",            "bet_id", "wager_amount"),
        ("prediction_contracts", "id",     "cost_bucks"),
        ("prop_wagers",          "id",     "wager_amount"),
    ]

    async with aiosqlite.connect(OLD_SB_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row

        # Step 1: Refund all Pending bets
        for table, id_col, wager_col in REFUND_SOURCES:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            row = await cur.fetchone()
            if not row:
                log.info(f"[CORE] Migration: {table} not found, skipping refund")
                continue
            cur = await db.execute(
                f"SELECT {id_col}, discord_id, {wager_col} FROM {table} WHERE status='Pending'")
            rows = await cur.fetchall()
            for r in rows:
                ref_key = f"MIGRATE_V7_REFUND_{table}_{r[id_col]}"
                await flow_wallet.credit(
                    int(r["discord_id"]), int(r[wager_col]), "MIGRATION",
                    description="Clean-break refund v7",
                    reference_key=ref_key)
                await db.execute(
                    f"UPDATE {table} SET status='Refunded' WHERE {id_col}=?", (r[id_col],))
            await db.commit()
            log.info(f"[CORE] Migration: refunded {len(rows)} Pending from {table}")

        # Step 2: Archive old tables
        ARCHIVE_TABLES = [
            "bets_table", "parlays_table", "parlay_legs",
            "real_events", "real_bets",
            "prediction_markets", "prediction_contracts",
            "prop_wagers",
        ]
        for tbl in ARCHIVE_TABLES:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,))
            row = await cur.fetchone()
            if not row:
                continue
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (f"{tbl}_arc_v7",))
            arc = await cur.fetchone()
            if arc:
                log.info(f"[CORE] Migration: {tbl}_arc_v7 already exists, skipping rename")
                continue
            await db.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_arc_v7")
            log.info(f"[CORE] Migration: archived {tbl} → {tbl}_arc_v7")
        await db.commit()

    # Step 3: Create new schema in flow.db
    await setup_db()
    log.warning("[CORE] Migration v7 complete — flow.db ready")


async def _post_settlement_card(event, bets) -> None:
    """Stub — post settlement ledger card. Implement when ledger integration is confirmed."""
    pass

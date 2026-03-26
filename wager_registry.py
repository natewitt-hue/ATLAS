"""
wager_registry.py -- ATLAS Unified Wager Registry
==================================================
Single write-through table for all wager lifecycle events across every
vertical: Sportsbook (straight bets, parlays, props), Casino, Predictions.

Every subsystem calls register_wager() at placement and settle_wager()
at resolution.  The wagers table enables cross-subsystem queries,
aggregate P&L reporting, and unified history views.

Follows flow_wallet.py conventions:
  - con=None (default): opens own connection, commits
  - con=<connection>: uses caller's connection, does NOT commit
  - Sync + async variants for sportsbook (sync sqlite3) vs casino/predictions (async aiosqlite)
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from flow_wallet import DB_PATH

log = logging.getLogger("wager_registry")

_DB_TIMEOUT = 10

# BUG-6 FIX: valid status transitions — prevents arbitrary overwrites
VALID_TRANSITIONS: dict[str, set[str]] = {
    "open":   {"won", "lost", "push", "voided", "cancelled"},
    "won":    {"voided"},           # admin correction only
    "lost":   {"voided"},           # admin correction only
    "push":   {"voided"},           # admin correction only
    "voided": set(),                # terminal state
    "cancelled": set(),             # terminal state
}


class InvalidTransitionError(Exception):
    """Raised when a wager status transition is not allowed."""
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
#  SCHEMA
# =============================================================================

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS wagers (
    wager_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subsystem      TEXT    NOT NULL,
    subsystem_id   TEXT    NOT NULL,
    discord_id     INTEGER NOT NULL,
    wager_amount   INTEGER NOT NULL,
    odds           INTEGER,
    label          TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'open',
    result_amount  INTEGER,
    created_at     TEXT    NOT NULL,
    settled_at     TEXT,
    UNIQUE(subsystem, subsystem_id)
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_wagers_user   ON wagers(discord_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_wagers_status ON wagers(status, subsystem)",
    "CREATE INDEX IF NOT EXISTS idx_wagers_sub    ON wagers(subsystem, subsystem_id)",
]


async def ensure_wager_table(db) -> None:
    """Create wagers table + indexes.  Called from flow_wallet.setup_wallet_db()."""
    await db.execute(_CREATE_TABLE)
    for idx in _INDEXES:
        await db.execute(idx)


# =============================================================================
#  ASYNC API
# =============================================================================

async def register_wager(
    subsystem: str,
    subsystem_id: str,
    discord_id: int,
    wager_amount: int,
    label: str = "",
    odds: Optional[int] = None,
    *,
    created_at: Optional[str] = None,
    con=None,
) -> int:
    """Register a new wager.  Returns wager_id.  Pass con= to join caller's txn."""
    ts = created_at or _now()

    async def _do(db):
        await db.execute(
            "INSERT OR IGNORE INTO wagers "
            "(subsystem, subsystem_id, discord_id, wager_amount, odds, label, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (subsystem, subsystem_id, discord_id, wager_amount, odds, label, ts),
        )
        async with db.execute(
            "SELECT wager_id FROM wagers WHERE subsystem=? AND subsystem_id=?",
            (subsystem, subsystem_id),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    if con is not None:
        return await _do(con)

    async with aiosqlite.connect(DB_PATH) as db:
        wid = await _do(db)
        await db.commit()
    return wid


async def settle_wager(
    subsystem: str,
    subsystem_id: str,
    status: str,
    result_amount: int,
    *,
    con=None,
) -> None:
    """Settle a wager by its composite key (subsystem, subsystem_id)."""
    ts = _now()

    async def _do(db):
        await db.execute(
            "UPDATE wagers SET status=?, result_amount=?, settled_at=? "
            "WHERE subsystem=? AND subsystem_id=? AND status='open'",
            (status, result_amount, ts, subsystem, subsystem_id),
        )

    if con is not None:
        await _do(con)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await _do(db)
        await db.commit()


async def update_wager_status(
    subsystem: str,
    subsystem_id: str,
    new_status: str,
    result_amount: Optional[int] = None,
    *,
    con=None,
) -> None:
    """Update wager status with transition validation (BUG-6 FIX)."""
    ts = _now()

    async def _do(db):
        async with db.execute(
            "SELECT status FROM wagers WHERE subsystem=? AND subsystem_id=?",
            (subsystem, subsystem_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError(f"Wager not found: {subsystem}/{subsystem_id}")
        current = row[0]
        allowed = VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:  # BUG-6 FIX: enforce valid transitions
            raise InvalidTransitionError(
                f"Cannot transition wager from '{current}' to '{new_status}'"
            )
        await db.execute(
            "UPDATE wagers SET status=?, result_amount=COALESCE(?, result_amount), settled_at=? "
            "WHERE subsystem=? AND subsystem_id=?",
            (new_status, result_amount, ts, subsystem, subsystem_id),
        )

    if con is not None:
        await _do(con)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await _do(db)
        await db.commit()


# =============================================================================
#  SYNC API (for flow_sportsbook.py)
# =============================================================================

def _db_con_sync():
    con = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def register_wager_sync(
    subsystem: str,
    subsystem_id: str,
    discord_id: int,
    wager_amount: int,
    label: str = "",
    odds: Optional[int] = None,
    *,
    created_at: Optional[str] = None,
    con: Optional[sqlite3.Connection] = None,
) -> int:
    """Sync version of register_wager."""
    ts = created_at or _now()

    def _do(c):
        c.execute(
            "INSERT OR IGNORE INTO wagers "
            "(subsystem, subsystem_id, discord_id, wager_amount, odds, label, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (subsystem, subsystem_id, discord_id, wager_amount, odds, label, ts),
        )
        row = c.execute(
            "SELECT wager_id FROM wagers WHERE subsystem=? AND subsystem_id=?",
            (subsystem, subsystem_id),
        ).fetchone()
        return row[0] if row else 0

    if con is not None:
        return _do(con)

    with _db_con_sync() as c:
        wid = _do(c)
        c.commit()
    return wid


def settle_wager_sync(
    subsystem: str,
    subsystem_id: str,
    status: str,
    result_amount: int,
    *,
    con: Optional[sqlite3.Connection] = None,
) -> None:
    """Sync version of settle_wager."""
    ts = _now()

    def _do(c):
        c.execute(
            "UPDATE wagers SET status=?, result_amount=?, settled_at=? "
            "WHERE subsystem=? AND subsystem_id=? AND status='open'",
            (status, result_amount, ts, subsystem, subsystem_id),
        )

    if con is not None:
        _do(con)
        return

    with _db_con_sync() as c:
        _do(c)
        c.commit()


def update_wager_status_sync(
    subsystem: str,
    subsystem_id: str,
    new_status: str,
    result_amount: Optional[int] = None,
    *,
    con: Optional[sqlite3.Connection] = None,
) -> None:
    """Sync version of update_wager_status with transition validation (BUG-6 FIX)."""
    ts = _now()

    def _do(c):
        row = c.execute(
            "SELECT status FROM wagers WHERE subsystem=? AND subsystem_id=?",
            (subsystem, subsystem_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Wager not found: {subsystem}/{subsystem_id}")
        current = row[0]
        allowed = VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:  # BUG-6 FIX: enforce valid transitions
            raise InvalidTransitionError(
                f"Cannot transition wager from '{current}' to '{new_status}'"
            )
        c.execute(
            "UPDATE wagers SET status=?, result_amount=COALESCE(?, result_amount), settled_at=? "
            "WHERE subsystem=? AND subsystem_id=?",
            (new_status, result_amount, ts, subsystem, subsystem_id),
        )

    if con is not None:
        _do(con)
        return

    with _db_con_sync() as c:
        _do(c)
        c.commit()


# =============================================================================
#  QUERY API (for display consumers)
# =============================================================================

async def get_active_wagers(discord_id: int) -> list[dict]:
    """All open wagers for a user, across all subsystems."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM wagers WHERE discord_id=? AND status='open' "
            "ORDER BY created_at DESC",
            (discord_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_wager_history(discord_id: int, limit: int = 50) -> list[dict]:
    """Recent settled wagers, newest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM wagers WHERE discord_id=? AND status != 'open' "
            "ORDER BY settled_at DESC LIMIT ?",
            (discord_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_wager_summary(discord_id: int) -> dict:
    """Aggregate stats: total wagered, total P&L, record, by subsystem."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT subsystem, "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins, "
            "  SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) as losses, "
            "  SUM(CASE WHEN status='push' THEN 1 ELSE 0 END) as pushes, "
            "  SUM(wager_amount) as total_wagered, "
            "  SUM(COALESCE(result_amount, 0)) as net_pnl "
            "FROM wagers WHERE discord_id=? AND status != 'open' "
            "GROUP BY subsystem",
            (discord_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["subsystem"]: dict(r) for r in rows}


# =============================================================================
#  BACKFILL MIGRATION
# =============================================================================

async def backfill_wagers() -> int:
    """
    One-time migration: populate wagers table from source tables.
    Idempotent via INSERT OR IGNORE + UNIQUE(subsystem, subsystem_id).
    """
    from odds_utils import payout_calc

    async with aiosqlite.connect(DB_PATH) as db:
        # Early-exit optimization (INSERT OR IGNORE is the real guard)
        async with db.execute("SELECT COUNT(*) FROM wagers") as cur:
            row = await cur.fetchone()
            if row and row[0] > 0:
                return 0

        total = 0

        # 1. Straight bets (exclude parlay leg rows)
        async with db.execute(
            "SELECT bet_id, discord_id, wager_amount, odds, pick, bet_type, "
            "status, created_at FROM bets_table WHERE parlay_id IS NULL"
        ) as cur:
            for bid, uid, amt, odds, pick, btype, status, created in await cur.fetchall():
                s = (status or "Pending").lower()
                if s == "pending":
                    s = "open"
                elif s == "cancelled":
                    s = "voided"
                result_amount = None
                settled_at = None
                if s == "won":
                    result_amount = payout_calc(amt, odds) - amt
                    settled_at = created  # approximate
                elif s == "lost":
                    result_amount = -amt
                    settled_at = created
                elif s in ("push", "voided"):
                    result_amount = 0
                    settled_at = created
                await db.execute(
                    "INSERT OR IGNORE INTO wagers "
                    "(subsystem, subsystem_id, discord_id, wager_amount, odds, "
                    "label, status, result_amount, created_at, settled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("TSL_BET", str(bid), uid, amt, odds,
                     f"{pick} {btype}", s, result_amount, created or _now(), settled_at),
                )
                total += 1

        # 2. Parlays
        async with db.execute(
            "SELECT parlay_id, discord_id, wager_amount, combined_odds, "
            "status, created_at FROM parlays_table"
        ) as cur:
            for pid, uid, amt, c_odds, status, created in await cur.fetchall():
                s = (status or "Pending").lower()
                if s == "pending":
                    s = "open"
                elif s == "cancelled":
                    s = "voided"
                result_amount = None
                settled_at = None
                if s == "won":
                    result_amount = payout_calc(amt, c_odds) - amt
                    settled_at = created
                elif s == "lost":
                    result_amount = -amt
                    settled_at = created
                elif s in ("push", "voided"):
                    result_amount = 0
                    settled_at = created
                await db.execute(
                    "INSERT OR IGNORE INTO wagers "
                    "(subsystem, subsystem_id, discord_id, wager_amount, odds, "
                    "label, status, result_amount, created_at, settled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("PARLAY", str(pid), uid, amt, c_odds,
                     "Parlay", s, result_amount, created or _now(), settled_at),
                )
                total += 1

        # 3. Prop wagers
        async with db.execute(
            "SELECT id, discord_id, wager_amount, odds, pick, status, placed_at "
            "FROM prop_wagers"
        ) as cur:
            for wid, uid, amt, odds, pick, status, placed in await cur.fetchall():
                s = (status or "Pending").lower()
                if s == "pending":
                    s = "open"
                elif s == "cancelled":
                    s = "voided"
                result_amount = None
                settled_at = None
                if s == "won":
                    result_amount = payout_calc(amt, odds) - amt
                    settled_at = placed
                elif s == "lost":
                    result_amount = -amt
                    settled_at = placed
                elif s in ("push", "voided"):
                    result_amount = 0
                    settled_at = placed
                await db.execute(
                    "INSERT OR IGNORE INTO wagers "
                    "(subsystem, subsystem_id, discord_id, wager_amount, odds, "
                    "label, status, result_amount, created_at, settled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("PROP", str(wid), uid, amt, odds,
                     f"Prop: {pick}", s, result_amount, placed or _now(), settled_at),
                )
                total += 1

        # 4. Casino sessions (all settled by definition)
        async with db.execute(
            "SELECT session_id, discord_id, game_type, wager, outcome, payout, played_at "
            "FROM casino_sessions"
        ) as cur:
            for sid, uid, gtype, wager_amt, outcome, payout, played in await cur.fetchall():
                s = "won" if outcome == "win" else ("push" if outcome == "push" else "lost")
                net = payout - wager_amt
                await db.execute(
                    "INSERT OR IGNORE INTO wagers "
                    "(subsystem, subsystem_id, discord_id, wager_amount, "
                    "label, status, result_amount, created_at, settled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("CASINO", str(sid), uid, wager_amt,
                     f"Casino {gtype}", s, net, played or _now(), played or _now()),
                )
                total += 1

        # 5. Prediction contracts
        async with db.execute(
            "SELECT id, user_id, slug, side, buy_price, cost_bucks, "
            "potential_payout, status, created_at, resolved_at "
            "FROM prediction_contracts"
        ) as cur:
            for cid, user_id, slug, side, price, cost, payout, status, created, resolved in await cur.fetchall():
                s = status or "open"
                result_amount = None
                settled_at = resolved
                if s == "won":
                    result_amount = payout - cost
                elif s == "lost":
                    result_amount = -cost
                elif s == "voided":
                    result_amount = 0
                elif s == "open":
                    settled_at = None
                await db.execute(
                    "INSERT OR IGNORE INTO wagers "
                    "(subsystem, subsystem_id, discord_id, wager_amount, "
                    "label, status, result_amount, created_at, settled_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    ("PREDICTION", str(cid), int(user_id), cost,
                     f"{slug}: {side} @ ${price:.2f}", s, result_amount,
                     created or _now(), settled_at),
                )
                total += 1

        await db.commit()
    return total


# =============================================================================
#  GAP 7 BACKFILLS — PvP coinflip + jackpot synthetic wagers
# =============================================================================

async def backfill_pvp_wagers() -> int:
    """
    Reclassify PvP coinflip sessions from CASINO → CASINO_PVP in wagers table,
    and insert any missing PvP sessions. Idempotent.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        total = 0

        # Step 1: Reclassify existing CASINO entries for coinflip_pvp sessions
        async with db.execute("""
            SELECT w.subsystem_id, cs.outcome, cs.payout, cs.wager
            FROM wagers w
            JOIN casino_sessions cs ON w.subsystem_id = CAST(cs.session_id AS TEXT)
            WHERE w.subsystem = 'CASINO'
              AND cs.game_type = 'coinflip_pvp'
        """) as cur:
            rows = await cur.fetchall()

        for sub_id, outcome, payout, wager in rows:
            pvp_sub_id = f"PVP_LEGACY_{sub_id}"
            status = "won" if outcome == "win" else "lost"
            result_amt = (payout - wager) if outcome == "win" else -wager
            await db.execute(
                "UPDATE wagers SET subsystem='CASINO_PVP', subsystem_id=?, "
                "label='Coinflip PvP', status=?, result_amount=? "
                "WHERE subsystem='CASINO' AND subsystem_id=?",
                (pvp_sub_id, status, result_amt, sub_id),
            )
            total += 1

        # Step 2: Insert any PvP sessions that weren't in wagers at all
        async with db.execute("""
            SELECT cs.session_id, cs.discord_id, cs.wager, cs.outcome,
                   cs.payout, cs.played_at
            FROM casino_sessions cs
            WHERE cs.game_type = 'coinflip_pvp'
              AND NOT EXISTS (
                  SELECT 1 FROM wagers w
                  WHERE w.subsystem = 'CASINO_PVP'
                    AND w.subsystem_id = 'PVP_LEGACY_' || CAST(cs.session_id AS TEXT)
              )
        """) as cur:
            missing = await cur.fetchall()

        for sid, uid, wager, outcome, payout, played in missing:
            sub_id = f"PVP_LEGACY_{sid}"
            status = "won" if outcome == "win" else "lost"
            result_amt = (payout - wager) if outcome == "win" else -wager
            await db.execute(
                "INSERT OR IGNORE INTO wagers "
                "(subsystem, subsystem_id, discord_id, wager_amount, "
                "label, status, result_amount, created_at, settled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("CASINO_PVP", sub_id, uid, wager,
                 "Coinflip PvP", status, result_amt,
                 played or _now(), played or _now()),
            )
            total += 1

        await db.commit()
    return total


async def backfill_jackpot_wagers() -> int:
    """
    Insert synthetic CASINO_JACKPOT wager entries for historical jackpot payouts.
    Idempotent via INSERT OR IGNORE + UNIQUE(subsystem, subsystem_id).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, tier, discord_id, amount, won_at
            FROM casino_jackpot_log
        """) as cur:
            rows = await cur.fetchall()

        total = 0
        for log_id, tier, uid, amount, won_at in rows:
            sub_id = f"JP_LEGACY_{log_id}"
            await db.execute(
                "INSERT OR IGNORE INTO wagers "
                "(subsystem, subsystem_id, discord_id, wager_amount, "
                "label, status, result_amount, created_at, settled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("CASINO_JACKPOT", sub_id, uid, 0,
                 f"Jackpot {tier.upper()}", "won", amount,
                 won_at or _now(), won_at or _now()),
            )
            total += 1

        await db.commit()
    return total

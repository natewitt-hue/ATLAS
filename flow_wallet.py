"""
flow_wallet.py -- ATLAS Flow Economy Unified Wallet Layer
=========================================================
Single source of truth for all balance operations across every vertical:
TSL Sportsbook, Casino, Prediction Markets, Real Sports Sportsbook, Economy.

Every module calls this instead of directly touching users_table.balance.

All async functions accept an optional `con` parameter:
  - con=None (default): opens own connection, BEGIN IMMEDIATE, commits
  - con=<aiosqlite.Connection>: uses caller's connection, does NOT commit
    (caller owns the transaction lifecycle)

Sync wrappers provided for flow_sportsbook.py (sync sqlite3 pattern).
=========================================================
"""

import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

# -- Per-user asyncio locks to serialize balance operations ------------------
_user_locks: dict[int, asyncio.Lock] = {}


def get_user_lock(uid: int) -> asyncio.Lock:
    """Get or create a per-user asyncio lock to serialize balance operations."""
    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    return _user_locks[uid]

# -- DB Path -----------------------------------------------------------------
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv(
    "FLOW_DB_PATH",
    os.path.join(_DIR, "flow_economy.db"),
)

STARTING_BALANCE = 1000
_DB_TIMEOUT = 10


class InsufficientFundsError(Exception):
    """Raised when a debit exceeds available balance."""
    pass


# =============================================================================
#  INTERNAL HELPERS
# =============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_user(db, discord_id: int) -> int:
    """Return current balance, creating user if needed. Must be inside a txn."""
    async with db.execute(
        "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO users_table (discord_id, balance, season_start_balance) "
            "VALUES (?, ?, ?)",
            (discord_id, STARTING_BALANCE, STARTING_BALANCE),
        )
        return STARTING_BALANCE
    return row[0]


def _ensure_user_sync(con, discord_id: int) -> int:
    """Sync version of _ensure_user. Must be inside a txn."""
    row = con.execute(
        "SELECT balance FROM users_table WHERE discord_id=?", (discord_id,)
    ).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO users_table (discord_id, balance, season_start_balance) "
            "VALUES (?, ?, ?)",
            (discord_id, STARTING_BALANCE, STARTING_BALANCE),
        )
        return STARTING_BALANCE
    return row[0]


async def _check_idempotent(db, reference_key: str) -> Optional[int]:
    """If reference_key already processed, return current balance. Else None."""
    if not reference_key:
        return None
    async with db.execute(
        "SELECT balance_after FROM transactions WHERE reference_key=?",
        (reference_key,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _insert_txn(
    db, discord_id: int, amount: int, balance_after: int,
    source: str, reference_key: Optional[str], description: str,
):
    """Insert a transaction record."""
    await db.execute(
        "INSERT INTO transactions "
        "(discord_id, amount, balance_after, source, reference_key, description, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (discord_id, amount, balance_after, source, reference_key, description, _now()),
    )


# =============================================================================
#  ASYNC PUBLIC API
# =============================================================================

async def get_balance(discord_id: int, *, con=None) -> int:
    """Read balance, auto-create user at STARTING_BALANCE if needed."""
    if con is not None:
        return await _ensure_user(con, discord_id)

    async with aiosqlite.connect(DB_PATH) as db:
        bal = await _ensure_user(db, discord_id)
        await db.commit()
    return bal


async def credit(
    discord_id: int,
    amount: int,
    source: str,
    description: str = "",
    reference_key: Optional[str] = None,
    *,
    con=None,
) -> int:
    """
    Add funds to a user's balance. Returns new balance.
    Idempotent if reference_key is provided and already exists.
    If con is provided, uses that connection (no commit).
    """
    if amount <= 0:
        raise ValueError(f"credit amount must be positive, got {amount}")

    if con is not None:
        # Caller owns the transaction
        existing = await _check_idempotent(con, reference_key)
        if existing is not None:
            return existing
        current = await _ensure_user(con, discord_id)
        new_balance = current + amount
        await con.execute(
            "UPDATE users_table SET balance=? WHERE discord_id=?",
            (new_balance, discord_id),
        )
        await _insert_txn(
            con, discord_id, amount, new_balance,
            source, reference_key, description,
        )
        return new_balance

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            existing = await _check_idempotent(db, reference_key)
            if existing is not None:
                await db.rollback()
                return existing
            current = await _ensure_user(db, discord_id)
            new_balance = current + amount
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id),
            )
            await _insert_txn(
                db, discord_id, amount, new_balance,
                source, reference_key, description,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return new_balance


async def debit(
    discord_id: int,
    amount: int,
    source: str,
    description: str = "",
    reference_key: Optional[str] = None,
    *,
    con=None,
) -> int:
    """
    Remove funds from a user's balance. Returns new balance.
    Raises InsufficientFundsError if balance < amount.
    If con is provided, uses that connection (no commit).
    """
    if amount <= 0:
        raise ValueError(f"debit amount must be positive, got {amount}")

    if con is not None:
        existing = await _check_idempotent(con, reference_key)
        if existing is not None:
            return existing
        current = await _ensure_user(con, discord_id)
        if current < amount:
            raise InsufficientFundsError(
                f"Balance {current:,} < wager {amount:,}"
            )
        new_balance = current - amount
        await con.execute(
            "UPDATE users_table SET balance=? WHERE discord_id=?",
            (new_balance, discord_id),
        )
        await _insert_txn(
            con, discord_id, -amount, new_balance,
            source, reference_key, description,
        )
        return new_balance

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            existing = await _check_idempotent(db, reference_key)
            if existing is not None:
                await db.rollback()
                return existing
            current = await _ensure_user(db, discord_id)
            if current < amount:
                await db.rollback()
                raise InsufficientFundsError(
                    f"Balance {current:,} < wager {amount:,}"
                )
            new_balance = current - amount
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (new_balance, discord_id),
            )
            await _insert_txn(
                db, discord_id, -amount, new_balance,
                source, reference_key, description,
            )
            await db.commit()
        except InsufficientFundsError:
            raise
        except Exception:
            await db.rollback()
            raise
    return new_balance


async def set_balance(
    discord_id: int,
    amount: int,
    source: str,
    description: str = "",
    *,
    con=None,
) -> tuple[int, int]:
    """
    Admin override: set balance to exact value.
    Returns (old_balance, new_balance).
    """
    if con is not None:
        old = await _ensure_user(con, discord_id)
        await con.execute(
            "UPDATE users_table SET balance=? WHERE discord_id=?",
            (amount, discord_id),
        )
        delta = amount - old
        await _insert_txn(
            con, discord_id, delta, amount,
            source, None, description,
        )
        return old, amount

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            old = await _ensure_user(db, discord_id)
            await db.execute(
                "UPDATE users_table SET balance=? WHERE discord_id=?",
                (amount, discord_id),
            )
            delta = amount - old
            await _insert_txn(
                db, discord_id, delta, amount,
                source, None, description,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return old, amount


async def get_last_txn_id(discord_id: int) -> Optional[int]:
    """Return the most recent txn_id for a user, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT txn_id FROM transactions "
            "WHERE discord_id=? ORDER BY txn_id DESC LIMIT 1",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def get_transactions(
    discord_id: int,
    limit: int = 20,
    source: Optional[str] = None,
) -> list[dict]:
    """Return recent transaction history for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if source:
            async with db.execute(
                "SELECT * FROM transactions "
                "WHERE discord_id=? AND source=? "
                "ORDER BY created_at DESC LIMIT ?",
                (discord_id, source, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM transactions "
                "WHERE discord_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (discord_id, limit),
            ) as cur:
                rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_leaderboard(limit: int = 15) -> list[dict]:
    """Return top N users by balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT discord_id, balance, season_start_balance "
            "FROM users_table ORDER BY balance DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_total_supply() -> int:
    """Return sum of all balances (total money in circulation)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM users_table"
        ) as cur:
            row = await cur.fetchone()
    return row[0]


async def setup_wallet_db() -> None:
    """
    Create transactions table + sportsbook_settings if missing.
    Called on bot startup.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                txn_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id    INTEGER NOT NULL,
                amount        INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                source        TEXT    NOT NULL,
                reference_key TEXT    UNIQUE DEFAULT NULL,
                description   TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_user "
            "ON transactions(discord_id, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_ref "
            "ON transactions(reference_key)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sportsbook_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.commit()


# =============================================================================
#  SYNC WRAPPERS (for flow_sportsbook.py)
# =============================================================================

def _db_con_sync():
    """Open a sync sqlite3 connection with WAL + timeout."""
    con = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def get_balance_sync(discord_id: int) -> int:
    """Sync version: read balance, auto-create user if needed."""
    with _db_con_sync() as con:
        return _ensure_user_sync(con, discord_id)


def update_balance_sync(
    discord_id: int,
    delta: int,
    source: str = "TSL_BET",
    description: str = "",
    reference_key: Optional[str] = None,
    con: Optional[sqlite3.Connection] = None,
) -> int:
    """
    Sync version: add/subtract from balance + log transaction.
    If con is provided, uses that connection (no commit).
    """
    def _run(c):
        # Idempotency check
        if reference_key:
            row = c.execute(
                "SELECT balance_after FROM transactions WHERE reference_key=?",
                (reference_key,),
            ).fetchone()
            if row:
                return row[0]

        current = _ensure_user_sync(c, discord_id)
        new_balance = current + delta
        if new_balance < 0:
            raise InsufficientFundsError(
                f"Balance {current:,} < debit {abs(delta):,}"
            )
        c.execute(
            "UPDATE users_table SET balance=? WHERE discord_id=?",
            (new_balance, discord_id),
        )
        c.execute(
            "INSERT INTO transactions "
            "(discord_id, amount, balance_after, source, reference_key, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (discord_id, delta, new_balance, source, reference_key, description, _now()),
        )
        return new_balance

    if con is not None:
        return _run(con)
    else:
        with _db_con_sync() as c:
            c.execute("BEGIN IMMEDIATE")
            result = _run(c)
            c.commit()
            return result

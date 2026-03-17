# oracle_memory.py
"""
Oracle v3 Permanent Memory — conversation storage, retrieval, and search.

Stores every Q&A pair permanently with:
  - FTS5 keyword search
  - Vector embeddings (populated in Phase 2)
  - Discord message ID for reply threading
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any


def init_memory_tables(db_path: str) -> None:
    """Create conversation_memory, oracle_query_log, and FTS5 tables."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_memory (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id    INTEGER NOT NULL,
            message_id    INTEGER,
            question      TEXT    NOT NULL,
            sql_query     TEXT,
            answer        TEXT    NOT NULL,
            tier          INTEGER DEFAULT 3,
            intent        TEXT,
            entities      TEXT,
            created_at    REAL    NOT NULL,
            embedding     BLOB
        );

        CREATE INDEX IF NOT EXISTS idx_mem_user_time
            ON conversation_memory(discord_id, created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_mem_message_id
            ON conversation_memory(message_id);

        CREATE TABLE IF NOT EXISTS oracle_query_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id      INTEGER NOT NULL,
            question        TEXT    NOT NULL,
            tier            INTEGER NOT NULL,
            intent          TEXT,
            model           TEXT,
            latency_ms      INTEGER NOT NULL,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            estimated_cost  REAL,
            sql_executed    TEXT,
            rows_returned   INTEGER,
            success         INTEGER DEFAULT 1,
            error_message   TEXT,
            created_at      REAL    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_log_time
            ON oracle_query_log(created_at DESC);
    """)

    # FTS5 virtual table (must be created separately — can't use IF NOT EXISTS)
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE conversation_memory_fts USING fts5(
                question, answer, entities,
                content='conversation_memory',
                content_rowid='id'
            )
        """)
    except sqlite3.OperationalError:
        pass  # Already exists

    conn.close()


def store_memory(
    db_path: str,
    discord_id: int,
    message_id: int | None,
    question: str,
    sql_query: str | None,
    answer: str,
    tier: int,
    intent: str | None,
    entities: str | None,
    embedding: bytes | None = None,
) -> int:
    """Store a conversation turn. Returns the row ID."""
    conn = sqlite3.connect(db_path, timeout=5)
    now = time.time()
    cur = conn.execute(
        """INSERT INTO conversation_memory
           (discord_id, message_id, question, sql_query, answer, tier, intent,
            entities, created_at, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (discord_id, message_id, question, sql_query, answer, tier, intent,
         entities, now, embedding),
    )
    row_id = cur.lastrowid

    # Sync FTS5 index
    conn.execute(
        """INSERT INTO conversation_memory_fts(rowid, question, answer, entities)
           VALUES (?, ?, ?, ?)""",
        (row_id, question, answer, entities or ""),
    )

    conn.commit()
    conn.close()
    return row_id


def get_recent_turns(
    db_path: str,
    discord_id: int,
    limit: int = 5,
) -> list[dict]:
    """Get the most recent conversation turns for a user."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT id, discord_id, message_id, question, sql_query, answer,
                  tier, intent, entities, created_at
           FROM conversation_memory
           WHERE discord_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (discord_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_turn_by_message_id(
    db_path: str,
    message_id: int,
) -> dict | None:
    """Look up a conversation turn by Discord message ID (for reply threading)."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT id, discord_id, message_id, question, sql_query, answer,
                  tier, intent, entities, created_at
           FROM conversation_memory
           WHERE message_id = ?""",
        (message_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def search_memory_fts(
    db_path: str,
    discord_id: int,
    query: str,
    limit: int = 3,
) -> list[dict]:
    """Search conversation memory via FTS5 keyword matching."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT cm.id, cm.question, cm.answer, cm.created_at,
                  rank AS relevance
           FROM conversation_memory_fts fts
           JOIN conversation_memory cm ON cm.id = fts.rowid
           WHERE conversation_memory_fts MATCH ?
             AND cm.discord_id = ?
           ORDER BY rank
           LIMIT ?""",
        (query, discord_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def log_query(
    db_path: str,
    discord_id: int,
    question: str,
    tier: int,
    intent: str | None,
    model: str | None,
    latency_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost: float | None = None,
    sql_executed: str | None = None,
    rows_returned: int | None = None,
    success: bool = True,
    error_message: str | None = None,
) -> None:
    """Log a query to the observability table."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute(
        """INSERT INTO oracle_query_log
           (discord_id, question, tier, intent, model, latency_ms,
            input_tokens, output_tokens, estimated_cost,
            sql_executed, rows_returned, success, error_message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (discord_id, question, tier, intent, model, latency_ms,
         input_tokens, output_tokens, estimated_cost,
         sql_executed, rows_returned, 1 if success else 0, error_message,
         time.time()),
    )
    conn.commit()
    conn.close()

"""
conversation_memory.py — Shared Conversation History for ATLAS
================================================================
Per-user conversation tracking with in-memory cache + SQLite persistence.

Source configs:
  casual — 10 turns, 24-hour TTL  (bot.py @mention chat)
  codex  — 5 turns,  30-minute TTL (legacy, kept for old DB rows)
  oracle — 5 turns,  30-minute TTL (Oracle modals + /ask — cross-modal sharing)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")

# ── Per-source configuration ─────────────────────────────────────────────────
_SOURCE_CONFIG: dict[str, dict] = {
    "casual": {"max_turns": 10, "ttl_seconds": 86400},    # 24 hours
    "codex":  {"max_turns": 5,  "ttl_seconds": 1800},     # 30 minutes (legacy)
    "oracle": {"max_turns": 5,  "ttl_seconds": 1800},     # 30 minutes — Oracle modals + /ask
}
_DEFAULT_CONFIG = {"max_turns": 10, "ttl_seconds": 86400}


def _config(source: str) -> dict:
    return _SOURCE_CONFIG.get(source, _DEFAULT_CONFIG)


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class ConversationTurn:
    question: str
    answer: str
    sql: str = ""
    source: str = "casual"
    timestamp: float = field(default_factory=time.time)


# ── In-memory cache: discord_id → list of recent turns ────────────────────────
_conv_cache: dict[int, list[ConversationTurn]] = {}

# ── Lazy DB init flag ─────────────────────────────────────────────────────────
_db_initialized = False


async def _ensure_db() -> None:
    """Create conversation_history table if it doesn't exist. Runs once."""
    global _db_initialized
    if _db_initialized:
        return
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id INTEGER NOT NULL,
                    question   TEXT    NOT NULL,
                    sql_query  TEXT,
                    answer     TEXT    NOT NULL,
                    source     TEXT    DEFAULT 'casual',
                    created_at REAL    NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_conv_user_time
                ON conversation_history(discord_id, created_at DESC)
            """)
            # Migration: add source column if table existed before this module
            try:
                await db.execute(
                    "ALTER TABLE conversation_history ADD COLUMN source TEXT DEFAULT 'casual'"
                )
            except Exception:
                pass  # Column already exists
            # Always attempt backfill — safe if already done (WHERE source IS NULL finds nothing)
            await db.execute(
                "UPDATE conversation_history SET source = 'codex' WHERE source IS NULL"
            )
            await db.commit()
        _db_initialized = True
        print("[ConversationMemory] conversation_history table ready")
    except Exception as e:
        print(f"[ConversationMemory] DB init error: {e}")


# ── Cache operations ──────────────────────────────────────────────────────────

def _get_cached_context(discord_id: int, source: str = "casual") -> list[ConversationTurn]:
    """Return recent non-stale turns from the in-memory cache."""
    cfg = _config(source)
    turns = _conv_cache.get(discord_id, [])
    cutoff = time.time() - cfg["ttl_seconds"]
    fresh = [t for t in turns if t.timestamp >= cutoff and t.source == source]
    # Evict only stale turns (across all sources) — don't drop other sources' data
    if len(fresh) != len([t for t in turns if t.source == source]):
        max_ttl = max(c["ttl_seconds"] for c in _SOURCE_CONFIG.values())
        global_cutoff = time.time() - max_ttl
        _conv_cache[discord_id] = [t for t in turns if t.timestamp >= global_cutoff]
    return fresh[-cfg["max_turns"]:]


# ── DB operations ─────────────────────────────────────────────────────────────

async def _load_from_db(discord_id: int, source: str = "casual") -> list[ConversationTurn]:
    """Cold-start recovery: load recent turns from SQLite."""
    await _ensure_db()
    cfg = _config(source)
    cutoff = time.time() - cfg["ttl_seconds"]
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT question, sql_query, answer, source, created_at "
                "FROM conversation_history "
                "WHERE discord_id = ? AND source = ? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (discord_id, source, cutoff, cfg["max_turns"]),
            )
            rows = list(await cursor.fetchall())
        return [
            ConversationTurn(
                question=r["question"],
                answer=r["answer"],
                sql=r["sql_query"] or "",
                source=r["source"] or source,
                timestamp=r["created_at"],
            )
            for r in reversed(rows)
        ]
    except Exception as e:
        print(f"[ConversationMemory] DB load error for {discord_id}/{source}: {e}")
        return []


async def add_conversation_turn(
    discord_id: int,
    question: str,
    answer: str,
    sql: str = "",
    source: str = "casual",
) -> None:
    """Store a turn in memory and persist to DB."""
    await _ensure_db()
    turn = ConversationTurn(
        question=question, answer=answer, sql=sql, source=source,
    )
    turns = _conv_cache.setdefault(discord_id, [])
    turns.append(turn)

    # Hysteresis trim: evict stale turns across all sources when list grows large
    max_total = sum(c["max_turns"] for c in _SOURCE_CONFIG.values()) * 2
    if len(turns) > max_total:
        max_ttl = max(c["ttl_seconds"] for c in _SOURCE_CONFIG.values())
        global_cutoff = time.time() - max_ttl
        _conv_cache[discord_id] = [t for t in turns if t.timestamp >= global_cutoff]

    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute(
                "INSERT INTO conversation_history "
                "(discord_id, question, sql_query, answer, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (discord_id, turn.question, turn.sql, turn.answer,
                 turn.source, turn.timestamp),
            )
            await db.commit()
    except Exception as e:
        print(f"[ConversationMemory] Persist error: {e}")


# ── Prompt builder ────────────────────────────────────────────────────────────

async def build_conversation_block(discord_id: int, source: str = "casual") -> str:
    """Build a conversation history string for prompt injection."""
    turns = _get_cached_context(discord_id, source)
    if not turns:
        turns = await _load_from_db(discord_id, source)
        if turns:
            _conv_cache[discord_id] = turns

    if not turns:
        return ""

    lines = ["RECENT CONVERSATION HISTORY (use for context and follow-up references):"]
    for i, t in enumerate(turns, 1):
        lines.append(f"  Q{i}: {t.question}")
        if t.sql:
            lines.append(f"  SQL{i}: {t.sql}")
        lines.append(f"  A{i}: {t.answer[:200]}")
    lines.append(
        "If the current question references 'that', 'those', 'them', 'the same', "
        "'it', 'he', 'she', 'they', etc., use the above history to resolve what is "
        "being referenced. You may reuse or modify previous SQL patterns if relevant."
    )
    return "\n".join(lines)

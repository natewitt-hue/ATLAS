"""
oracle_memory.py — Permanent Conversation Memory for Oracle v3
═══════════════════════════════════════════════════════════════════════════════
Permanent conversation memory with hybrid retrieval:
  1. Sliding window (most recent turns)
  2. FTS5 keyword search (BM25 ranking)
  3. Vector similarity search (Gemini text-embedding-004 cosine similarity)

Tables (in tsl_history.db alongside existing conversation_history):
  conversation_memory — permanent Q&A store with FTS5 + vector search
  oracle_query_log    — observability: model, latency, cost, tier, rows
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time

import aiosqlite

log = logging.getLogger("oracle_memory")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")

# ── Schema DDL ───────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
-- Permanent conversation memory (replaces TTL-based conversation_history)
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

CREATE INDEX IF NOT EXISTS idx_cmem_user_time
    ON conversation_memory(discord_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cmem_message
    ON conversation_memory(message_id);

-- Observability logging
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

CREATE INDEX IF NOT EXISTS idx_qlog_time
    ON oracle_query_log(created_at DESC);
"""

# FTS5 virtual table — created separately because CREATE VIRTUAL TABLE
# doesn't support IF NOT EXISTS in all SQLite versions
_FTS_SQL = """
CREATE VIRTUAL TABLE conversation_memory_fts USING fts5(
    question, answer, entities,
    content='conversation_memory', content_rowid='id'
);
"""

# Triggers to keep FTS index in sync with main table
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS cmem_ai AFTER INSERT ON conversation_memory BEGIN
    INSERT INTO conversation_memory_fts(rowid, question, answer, entities)
    VALUES (new.id, new.question, new.answer, new.entities);
END;

CREATE TRIGGER IF NOT EXISTS cmem_ad AFTER DELETE ON conversation_memory BEGIN
    INSERT INTO conversation_memory_fts(conversation_memory_fts, rowid, question, answer, entities)
    VALUES ('delete', old.id, old.question, old.answer, old.entities);
END;

CREATE TRIGGER IF NOT EXISTS cmem_au AFTER UPDATE ON conversation_memory BEGIN
    INSERT INTO conversation_memory_fts(conversation_memory_fts, rowid, question, answer, entities)
    VALUES ('delete', old.id, old.question, old.answer, old.entities);
    INSERT INTO conversation_memory_fts(rowid, question, answer, entities)
    VALUES (new.id, new.question, new.answer, new.entities);
END;
"""

# ── Initialization ───────────────────────────────────────────────────────────

_initialized = False


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _ensure_schema(db_path: str | None = None) -> None:
    """Create tables, FTS index, and triggers if they don't exist."""
    global _initialized
    if _initialized:
        return

    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path, timeout=10) as db:
            await db.execute("PRAGMA journal_mode=WAL")

            # Core tables + indexes
            for stmt in _SCHEMA_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)

            # FTS5 virtual table (may already exist)
            try:
                await db.execute(_FTS_SQL)
            except Exception:
                pass  # Already exists

            # Sync triggers
            for stmt in _FTS_TRIGGERS_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        await db.execute(stmt)
                    except Exception:
                        pass  # Already exists

            await db.commit()

        _initialized = True
        log.info("Oracle memory schema ready")

        # Auto-migrate from old conversation_history on first init
        mem = OracleMemory(path)
        migrated = await mem.migrate_from_conversation_history()
        if migrated:
            log.info("Auto-migrated %d turns from conversation_history", migrated)

    except Exception as e:
        log.error("Oracle memory schema init failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  ORACLE MEMORY CLASS
# ══════════════════════════════════════════════════════════════════════════════

class OracleMemory:
    """Permanent conversation memory with FTS5 search.

    Usage::

        memory = OracleMemory()
        await memory.store_turn(discord_id=123, question="...", answer="...")
        recent = await memory.get_recent(discord_id=123, limit=5)
        results = await memory.search_fts("passing yards", discord_id=123)
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or DB_PATH

    async def _ensure(self) -> None:
        await _ensure_schema(self._db_path)

    # ── Store ─────────────────────────────────────────────────────────────

    async def store_turn(
        self,
        discord_id: int,
        question: str,
        answer: str,
        *,
        sql: str | None = None,
        tier: int | None = None,
        intent: str | None = None,
        entities: dict | None = None,
        message_id: int | None = None,
        embedding: list[float] | None = None,
        created_at: float | None = None,
    ) -> int | None:
        """Store a conversation turn. Returns the row ID or None on error."""
        await self._ensure()
        entities_json = json.dumps(entities) if entities else None
        embedding_blob = json.dumps(embedding).encode() if embedding else None
        now = created_at or time.time()

        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                cursor = await db.execute(
                    "INSERT INTO conversation_memory "
                    "(discord_id, message_id, question, sql_query, answer, "
                    " tier, intent, entities, created_at, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (discord_id, message_id, question, sql, answer,
                     tier, intent, entities_json, now, embedding_blob),
                )
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            log.error("Failed to store turn: %s", e)
            return None

    async def embed_and_store(
        self,
        discord_id: int,
        question: str,
        answer: str,
        **kwargs,
    ) -> int | None:
        """Store a turn with an auto-generated embedding.

        Calls atlas_ai.embed_text() to generate a vector from the question,
        then stores the turn with the embedding. If embedding fails, the turn
        is still stored (just without a vector for similarity search).

        The timestamp is captured *before* the embedding call so that
        concurrent turns are stored in true chronological order even if
        embedding latencies vary.
        """
        # Capture timestamp before the (potentially slow) embedding call
        # so concurrent turns are ordered by question time, not store time.
        created_at = time.time()
        try:
            import atlas_ai
            embedding = await atlas_ai.embed_text(question)
        except Exception:
            embedding = None
        return await self.store_turn(
            discord_id, question, answer,
            embedding=embedding, created_at=created_at, **kwargs,
        )

    async def log_query(
        self,
        discord_id: int,
        question: str,
        tier: int,
        latency_ms: int,
        *,
        intent: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost: float | None = None,
        sql_executed: str | None = None,
        rows_returned: int | None = None,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        """Log a query for observability."""
        await self._ensure()
        now = time.time()

        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                await db.execute(
                    "INSERT INTO oracle_query_log "
                    "(discord_id, question, tier, intent, model, latency_ms, "
                    " input_tokens, output_tokens, estimated_cost, "
                    " sql_executed, rows_returned, success, error_message, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (discord_id, question, tier, intent, model, latency_ms,
                     input_tokens, output_tokens, estimated_cost,
                     sql_executed, rows_returned, 1 if success else 0,
                     error_message, now),
                )
                await db.commit()
        except Exception as e:
            log.error("Failed to log query: %s", e)

    # ── Retrieve ──────────────────────────────────────────────────────────

    async def get_recent(
        self,
        discord_id: int,
        limit: int = 5,
    ) -> list[dict]:
        """Get most recent turns for a user (sliding window)."""
        await self._ensure()
        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, question, sql_query, answer, tier, intent, "
                    "       entities, created_at, message_id "
                    "FROM conversation_memory "
                    "WHERE discord_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (discord_id, limit),
                )
                rows = await cursor.fetchall()
                return [dict(r) for r in reversed(rows)]  # Chronological order
        except Exception as e:
            log.error("Failed to get recent turns: %s", e)
            return []

    async def get_by_message_id(self, message_id: int) -> dict | None:
        """Look up a turn by its Discord message ID (for reply threading)."""
        await self._ensure()
        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, discord_id, question, sql_query, answer, "
                    "       tier, intent, entities, created_at "
                    "FROM conversation_memory "
                    "WHERE message_id = ? LIMIT 1",
                    (message_id,),
                )
                row = await cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            log.error("Failed to get turn by message_id: %s", e)
            return None

    @staticmethod
    def _sanitize_fts(query: str) -> str:
        """Strip FTS5 special characters so user input can't break MATCH syntax."""
        # Remove FTS5 operators: " * ? : ^ ( ) { } + - ~
        sanitized = re.sub(r'[\"*?:^(){}+\-~]', ' ', query)
        # Collapse whitespace and strip
        return re.sub(r'\s+', ' ', sanitized).strip()

    async def search_fts(
        self,
        query: str,
        discord_id: int | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Full-text search across conversation memory using BM25 ranking.

        Searches question, answer, and entities fields.
        Optionally scoped to a specific user.
        """
        await self._ensure()
        query = self._sanitize_fts(query)
        if not query:
            return []
        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row

                if discord_id is not None:
                    cursor = await db.execute(
                        "SELECT cm.id, cm.question, cm.answer, cm.sql_query, "
                        "       cm.tier, cm.entities, cm.created_at, "
                        "       rank "
                        "FROM conversation_memory_fts fts "
                        "JOIN conversation_memory cm ON cm.id = fts.rowid "
                        "WHERE conversation_memory_fts MATCH ? "
                        "  AND cm.discord_id = ? "
                        "ORDER BY rank "
                        "LIMIT ?",
                        (query, discord_id, limit),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT cm.id, cm.question, cm.answer, cm.sql_query, "
                        "       cm.tier, cm.entities, cm.created_at, "
                        "       rank "
                        "FROM conversation_memory_fts fts "
                        "JOIN conversation_memory cm ON cm.id = fts.rowid "
                        "WHERE conversation_memory_fts MATCH ? "
                        "ORDER BY rank "
                        "LIMIT ?",
                        (query, limit),
                    )
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.error("FTS search failed: %s", e)
            return []

    # ── Vector search ────────────────────────────────────────────────────

    async def search_vector(
        self,
        query_embedding: list[float],
        discord_id: int | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Vector similarity search via cosine similarity.

        Loads rows with embeddings, computes cosine similarity in Python,
        returns top-k. At ~36K rows/year this takes <100ms — no vector DB needed.
        """
        await self._ensure()
        if not query_embedding:
            return []

        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                db.row_factory = aiosqlite.Row

                if discord_id is not None:
                    cursor = await db.execute(
                        "SELECT id, discord_id, question, sql_query, answer, "
                        "       tier, entities, created_at, embedding "
                        "FROM conversation_memory "
                        "WHERE embedding IS NOT NULL AND discord_id = ? "
                        "ORDER BY created_at DESC LIMIT 2000",
                        (discord_id,),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT id, discord_id, question, sql_query, answer, "
                        "       tier, entities, created_at, embedding "
                        "FROM conversation_memory "
                        "WHERE embedding IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT 2000",
                    )
                rows = await cursor.fetchall()

            # Compute cosine similarity for each row
            scored: list[tuple[float, dict]] = []
            for row in rows:
                row_dict = dict(row)
                try:
                    stored_emb = json.loads(row_dict.pop("embedding"))
                    sim = _cosine_similarity(query_embedding, stored_emb)
                    scored.append((sim, row_dict))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

            # Sort by similarity (highest first), return top-k
            scored.sort(key=lambda x: x[0], reverse=True)
            return [row for _, row in scored[:limit]]

        except Exception as e:
            log.error("Vector search failed: %s", e)
            return []

    # ── Maintenance ───────────────────────────────────────────────────────

    async def prune_old_turns(self, days: int = 90) -> int:
        """Delete conversation_memory rows older than `days` days. Returns row count deleted."""
        await self._ensure()
        cutoff = time.time() - days * 86400
        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                cursor = await db.execute(
                    "DELETE FROM conversation_memory WHERE created_at < ?",
                    (cutoff,),
                )
                await db.commit()
                return cursor.rowcount
        except Exception as e:
            log.error("prune_old_turns failed: %s", e)
            return 0

    # ── Hybrid retrieval (Phase 2) ────────────────────────────────────────

    async def retrieve_context(
        self,
        discord_id: int,
        question: str,
        *,
        recent_limit: int = 5,
        fts_limit: int = 3,
        vector_limit: int = 3,
        max_turns: int = 8,
    ) -> list[dict]:
        """Hybrid retrieval: sliding window + FTS + vector.

        Returns up to max_turns deduplicated context turns, ranked
        by recency + relevance. Three retrieval signals:
          1. Sliding window — immediate conversational context
          2. FTS5 keyword search — exact keyword matches via BM25
          3. Vector similarity — semantic matches via cosine similarity
        """
        seen_ids: set[int] = set()
        results: list[dict] = []

        # 1. Sliding window (always included)
        recent = await self.get_recent(discord_id, limit=recent_limit)
        for r in recent:
            rid = r.get("id")
            if rid not in seen_ids:
                seen_ids.add(rid)
                r["_source"] = "recent"
                results.append(r)

        # 2. FTS keyword search
        try:
            fts_results = await self.search_fts(question, discord_id=discord_id, limit=fts_limit)
            for r in fts_results:
                rid = r.get("id")
                if rid not in seen_ids:
                    seen_ids.add(rid)
                    r["_source"] = "fts"
                    results.append(r)
        except Exception:
            pass  # FTS failure is non-fatal

        # 3. Vector similarity search
        try:
            import atlas_ai
            embedding = await atlas_ai.embed_text(question)
            if embedding:
                vector_results = await self.search_vector(
                    embedding, discord_id=discord_id, limit=vector_limit,
                )
                for r in vector_results:
                    rid = r.get("id")
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        r["_source"] = "vector"
                        results.append(r)
        except Exception:
            pass  # Vector search failure is non-fatal

        # Deduplicate and cap
        return results[:max_turns]

    async def build_context_block(
        self,
        discord_id: int,
        question: str,
    ) -> str:
        """Build a conversation context string for prompt injection.

        Replacement for conversation_memory.build_conversation_block().
        """
        turns = await self.retrieve_context(discord_id, question)
        if not turns:
            return ""

        lines = ["CONVERSATION HISTORY (permanent — use for context and follow-up references):"]
        for i, t in enumerate(turns, 1):
            lines.append(f"  Q{i}: {t['question']}")
            if t.get("sql_query"):
                lines.append(f"  SQL{i}: {t['sql_query']}")
            lines.append(f"  A{i}: {t['answer'][:200]}")
        lines.append(
            "If the current question references 'that', 'those', 'them', 'the same', "
            "'it', 'he', 'she', 'they', etc., use the above history to resolve what is "
            "being referenced. You may reuse or modify previous SQL patterns if relevant."
        )
        return "\n".join(lines)

    # ── Deletion ──────────────────────────────────────────────────────────

    async def forget_user(self, discord_id: int) -> int:
        """Delete all conversation memory for a user (/forget command).

        Returns number of rows deleted.
        """
        await self._ensure()
        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                cursor = await db.execute(
                    "DELETE FROM conversation_memory WHERE discord_id = ?",
                    (discord_id,),
                )
                count = cursor.rowcount
                await db.commit()
                return count
        except Exception as e:
            log.error("Failed to forget user %s: %s", discord_id, e)
            return 0

    # ── Stats ─────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        """Get memory system stats for monitoring."""
        await self._ensure()
        try:
            async with aiosqlite.connect(self._db_path, timeout=10) as db:
                row = await db.execute_fetchall(
                    "SELECT COUNT(*) as total, "
                    "       COUNT(DISTINCT discord_id) as users "
                    "FROM conversation_memory"
                )
                total, users = row[0] if row else (0, 0)

                log_row = await db.execute_fetchall(
                    "SELECT COUNT(*) as queries, "
                    "       AVG(latency_ms) as avg_latency, "
                    "       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as failures "
                    "FROM oracle_query_log"
                )
                queries, avg_latency, failures = log_row[0] if log_row else (0, 0, 0)

                return {
                    "total_turns": total,
                    "unique_users": users,
                    "total_queries": queries,
                    "avg_latency_ms": round(avg_latency or 0, 1),
                    "failure_count": failures or 0,
                }
        except Exception as e:
            log.error("Failed to get stats: %s", e)
            return {}

    # ── Migration ─────────────────────────────────────────────────────────

    async def migrate_from_conversation_history(self) -> int:
        """One-time migration from old conversation_history table.

        Copies all turns into conversation_memory (without embeddings).
        Idempotent: skips if conversation_memory already has rows.
        Returns number of rows migrated.
        """
        await self._ensure()
        try:
            async with aiosqlite.connect(self._db_path, timeout=30) as db:
                # Check if already migrated
                row = await db.execute_fetchall(
                    "SELECT COUNT(*) FROM conversation_memory"
                )
                if row and row[0][0] > 0:
                    log.info("Migration skipped — conversation_memory already has %d rows", row[0][0])
                    return 0

                # Check if source table exists
                tables = await db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_history'"
                )
                if not tables:
                    log.info("Migration skipped — conversation_history table not found")
                    return 0

                # Migrate all rows
                cursor = await db.execute(
                    "INSERT INTO conversation_memory "
                    "(discord_id, question, sql_query, answer, tier, created_at) "
                    "SELECT discord_id, question, sql_query, answer, "
                    "       CASE source "
                    "         WHEN 'codex' THEN 1 "
                    "         WHEN 'oracle' THEN 2 "
                    "         ELSE 3 "
                    "       END, "
                    "       created_at "
                    "FROM conversation_history "
                    "ORDER BY created_at"
                )
                count = cursor.rowcount
                await db.commit()
                log.info("Migrated %d turns from conversation_history → conversation_memory", count)
                return count
        except Exception as e:
            log.error("Migration failed: %s", e)
            return 0

# Casual Conversation Memory for ATLAS Echo

**Date:** 2026-03-19
**Status:** Draft

## Problem

ATLAS's casual @mention handler (`bot.py:482-531`) is fully stateless. Each message is processed independently — Gemini receives no context about prior exchanges. This causes ATLAS to fail on any follow-up question (e.g., "do you know who those people are" after an FMK game).

Meanwhile, the `/ask` command in `codex_cog.py` already has a working conversation history system with TTL, max turns, DB persistence, and prompt injection. It just isn't available to casual chat.

## Solution

Extract the conversation history system from `codex_cog.py` into a shared module (`conversation_memory.py`), then wire it into the casual @mention handler in `bot.py`. Both `/ask` and casual chat use the same infrastructure.

### How It Layers With Affinity

| System | Purpose | Persistence | Scope |
|--------|---------|-------------|-------|
| **Affinity** (`affinity.py`) | Long-term relationship tone (FRIEND/HOSTILE/etc.) | Permanent (DB score) | Per-user |
| **Conversation Memory** (new) | Short-term conversational context | 24h TTL, 10 turns | Per-user global |

Both inject into the Gemini prompt. Conversation history goes first, then affinity instruction, then lore context. Cross-channel bleed is intentional — a user is one person regardless of channel, and ATLAS should remember what they talked about.

## Design

### 1. New Module: `conversation_memory.py`

Extract from `codex_cog.py` lines 91-231 into a standalone module.

**DB path:** Module-internal default `os.path.join(os.path.dirname(__file__), "tsl_history.db")`, matching codex's existing path construction. Callers don't need to pass it.

**Dataclass:**
```python
@dataclass
class ConversationTurn:
    question: str
    answer: str
    sql: str = ""           # Only populated by /ask (codex)
    source: str = "casual"  # "casual" | "codex"
    timestamp: float = field(default_factory=time.time)
```

**Per-source configuration:**
```python
# Different contexts have different memory needs
_SOURCE_CONFIG = {
    "casual": {"max_turns": 10, "ttl_seconds": 86400},   # 24 hours
    "codex":  {"max_turns": 5,  "ttl_seconds": 1800},    # 30 minutes (unchanged)
}
DEFAULT_CONFIG = {"max_turns": 10, "ttl_seconds": 86400}
```

This preserves codex's existing 5-turn/30-min behavior while giving casual chat 10-turn/24-hour memory. The `get_conversation_context()` and `build_conversation_block()` functions accept a `source` parameter to select the right config.

**DB table** (same schema, additive `source` column):
```sql
CREATE TABLE IF NOT EXISTS conversation_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    question   TEXT    NOT NULL,
    sql_query  TEXT,
    answer     TEXT    NOT NULL,
    source     TEXT    DEFAULT 'casual',
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_user_time
    ON conversation_history(discord_id, created_at DESC);
```

**Migration for existing tables:** If table already exists, run:
```sql
ALTER TABLE conversation_history ADD COLUMN source TEXT DEFAULT 'casual';
UPDATE conversation_history SET source = 'codex' WHERE source IS NULL;
```

**Async DB access:** Use `aiosqlite` (already a project dependency via `affinity.py`) instead of synchronous `sqlite3` for all DB operations. This avoids blocking the event loop when called from `on_message`. The in-memory cache handles the hot path; DB is only hit on cold-start recovery.

**Answer truncation:** Answers in the conversation block are truncated to 200 characters (matching existing codex behavior). With 10 turns, this caps the conversation block at ~4KB.

**Thread safety:** The `_conv_cache` dict uses Python's GIL for atomic dict operations. The read-append-trim pattern in `add_conversation_turn` is safe under cooperative async (single-threaded event loop). No lock needed.

**Exported functions:**
- `init_conversation_db()` — create table if missing (called lazily on first use)
- `get_conversation_context(discord_id, source="casual")` — return fresh turns from cache
- `load_conversations_from_db(discord_id, source="casual")` — async cold-start recovery
- `add_conversation_turn(discord_id, question, answer, sql="", source="casual")` — store in cache + DB
- `build_conversation_block(discord_id, source="casual")` — async, build prompt string

**Prompt format:**
```
RECENT CONVERSATION HISTORY (use for context and follow-up references):
  Q1: fuck marry kill Jackie Kennedy, Viola Davis, Elena Kagan
  A1: fuck elena kagan, marry viola davis, kill jackie ke...
  Q2: do you know who those people are
  A2: ...
If the current question references 'that', 'those', 'them', 'the same',
'it', 'he', 'she', 'they', etc., use the above history to resolve what
is being referenced.
```

### 2. Wire Into `bot.py` on_message Handler

In `on_message()` (lines 482-531), add two integration points:

**Before the Gemini call, after affinity injection (line ~513):**
```python
# Build conversation context
conv_block = await build_conversation_block(message.author.id, source="casual")
if conv_block:
    context = f"{conv_block}\n\n{context}"
```

Note: `build_conversation_block` is now async (uses aiosqlite for cold-start). It prepends to the existing context which already has affinity + lore. Final order in Gemini prompt: `conversation_history → affinity_instruction → lore_context`.

**After the Gemini response, before affinity update (line ~517):**
```python
await add_conversation_turn(
    message.author.id, user_input, wit, source="casual"
)
```

**No separate init call needed** — the module auto-initializes on first DB access (lazy init with a flag, matching the existing codex pattern of retrying on "no such table").

### 3. Update `codex_cog.py`

Replace inline conversation functions (lines 91-231) with imports from `conversation_memory.py`. Update calls to pass `source="codex"` and `sql=generated_sql`. Codex keeps its existing 5-turn/30-min behavior via the source-specific config. No behavior change for `/ask`.

## Files Modified

| File | Change |
|------|--------|
| `conversation_memory.py` | **New** — extracted shared conversation history module |
| `bot.py` | Import module, add context + recording to `on_message` |
| `codex_cog.py` | Replace inline conversation code with imports |

## Verification

1. @mention ATLAS with a topic, then ask a follow-up referencing "that" or "those" — ATLAS should maintain context
2. Restart the bot, @mention ATLAS with a follow-up — cold-start should load recent turns from DB
3. Run `/ask` with follow-up questions — verify codex still works with 5-turn/30-min window (regression)
4. Check that affinity tiers still modify tone correctly alongside conversation context
5. Verify turns older than 24 hours are pruned from casual context
6. Check that cross-channel context works (mention in #general, follow up in #banter)

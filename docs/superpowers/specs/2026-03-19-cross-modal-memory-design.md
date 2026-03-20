# Cross-Modal Memory Completion — Design Spec

> **Priority:** #5 of 7 Oracle V4 improvements (was C7 in V4 handoff)
> **Date:** 2026-03-19
> **Scope:** `conversation_memory.py` (source filtering bug fix + new source), `oracle_cog.py` (source rename + _AskWebModal memory), `codex_cog.py` (source rename), `bot.py` (version bump)
> **Approach:** Fix source isolation bug, unify TSL modals under "oracle" source, add memory to _AskWebModal

---

## Problem

The V4 handoff (C7) identified two issues with conversation memory:

1. **Source siloing prevents cross-modal context.** A question asked via `/ask` (codex) doesn't inform Oracle modals. The fix: use a unified source for all TSL-related modals.

2. **_AskWebModal has no memory at all.** Open Intel and Sports Intel can't handle follow-ups like "tell me more about that" because they neither retrieve nor store conversation turns.

Additionally, during investigation a **source filtering bug** was found: `_get_cached_context()` and `_load_from_db()` don't filter by source, so casual @mention chat history leaks into codex/oracle contexts on cold starts.

**What's already done (from #1 and #4):** AskTSLModal, PlayerScoutModal, StrategyRoomModal, and `/ask` all store and retrieve memory using `source="codex"`. The infrastructure works — this task completes it.

---

## Design

### 1. Fix Source Filtering Bug (conversation_memory.py)

**`_get_cached_context()`** — add source filter to the list comprehension:
```python
fresh = [t for t in turns if t.timestamp >= cutoff and t.source == source]
```

**`_load_from_db()`** — add `AND source = ?` to the WHERE clause:
```python
"WHERE discord_id = ? AND source = ? AND created_at >= ? "
```

This ensures casual chat and oracle/codex contexts are properly isolated.

### 2. Add "oracle" Source Config (conversation_memory.py)

Add to `_SOURCE_CONFIG`:
```python
"oracle": {"max_turns": 5, "ttl_seconds": 1800},  # 30 minutes — same as codex
```

The "codex" config is kept for backwards compatibility (existing DB rows have `source='codex'`). New turns will use "oracle".

### 3. Rename Source in All TSL Modals (oracle_cog.py)

Change all `source="codex"` → `source="oracle"` in:
- AskTSLModal._generate() (2 locations: retrieve + store)
- PlayerScoutModal._generate() (2 locations)
- StrategyRoomModal._generate() (2 locations)

### 4. Rename Source in Codex /ask (codex_cog.py)

Change all `source="codex"` → `source="oracle"` in:
- `/ask` pipeline (3 locations: retrieve + 2 stores)

This ensures `/ask` and Oracle modals share context, which is the core goal of C7.

### 5. Add Memory to _AskWebModal (oracle_cog.py)

Add conversation memory retrieve + store to `_AskWebModal._generate()`:

```python
async def _generate(self, interaction: discord.Interaction) -> tuple[str, dict]:
    q = self.question.value.strip()

    # ── Conversation memory ────────────────────────────
    conv_block = ""
    if _build_conversation_block:
        conv_block = await _build_conversation_block(interaction.user.id, source="oracle")

    system_instruction = get_persona("analytical")
    contents = f"{conv_block}\n\n{q}" if conv_block else q
    result = await atlas_ai.generate_with_search(contents, system=system_instruction)

    # ... existing mode-specific logic ...

    answer = result.text or fallback_msg

    if _add_conversation_turn:
        await _add_conversation_turn(
            interaction.user.id, q, answer, sql="", source="oracle"
        )

    # ... existing footer logic ...
    return answer, {"title": embed_title, "color": embed_color, "footer": footer}
```

### 6. Quarantine Dead Files

Move to `QUARANTINE/`:
- `oracle_memory.py` — unused permanent memory system, never imported
- `tests/test_oracle_memory.py` — tests for dead code

### 7. Update Module Docstring (conversation_memory.py)

Update the docstring to reflect the new "oracle" source and its users.

---

## Files Modified

| File | Changes |
|------|---------|
| `conversation_memory.py` | Fix source filtering in `_get_cached_context()` + `_load_from_db()`, add "oracle" source config, update docstring |
| `oracle_cog.py` | `source="codex"` → `source="oracle"` (6 locations), add memory to `_AskWebModal._generate()` |
| `codex_cog.py` | `source="codex"` → `source="oracle"` (3 locations) |
| `bot.py` | Bump `ATLAS_VERSION` (3.9.0 → 3.10.0) |
| `oracle_memory.py` → `QUARANTINE/` | Dead code quarantine |
| `tests/test_oracle_memory.py` → `QUARANTINE/` | Dead test quarantine |

---

## What's NOT Changing

- **conversation_memory.py API** — `add_conversation_turn()` and `build_conversation_block()` signatures unchanged
- **"casual" source** — bot.py @mention chat keeps `source="casual"`, unaffected
- **"codex" config** — kept in `_SOURCE_CONFIG` for any existing DB rows (old turns expire via TTL anyway)
- **Non-intel modals** — H2H, TeamSearch, SeasonRecap are lookup-style, don't benefit from conversational memory
- **DB schema** — no migration needed, source column already exists

---

## Testing

1. Use Oracle Hub → AskTSL → "Who leads in passing yards?" → then PlayerScout → "Compare that player to..." → should see AskTSL context
2. Use Oracle Hub → Open Intel → "Latest NFL trade news" → follow up "Tell me more about that" → should have context
3. Use `/ask` → "Best QBs in the league" → then Oracle Hub → AskTSL → "How does that compare to last season?" → should share context
4. Casual @mention should NOT appear in Oracle modal context (source isolation)
5. Verify existing "codex" source DB rows are handled gracefully (TTL expiry, not loaded into "oracle")

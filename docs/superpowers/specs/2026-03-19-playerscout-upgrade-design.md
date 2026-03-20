# PlayerScout Upgrade — Design Spec

> **Priority:** #1 of 7 Oracle V4 improvements (was C5 in V4 handoff)
> **Date:** 2026-03-19
> **Scope:** oracle_cog.py `PlayerScoutModal` (~lines 3280-3393)
> **Approach:** Surgical in-place upgrade — no new files, no shared pipeline extraction (deferred to C1: Unified Modal Base)

---

## Problem

PlayerScoutModal is the only AI-powered Oracle modal with **zero error recovery**. When Haiku generates bad SQL against the players/player_abilities tables — which have unusual schema quirks (firstName/lastName instead of fullName, 800+ free agents with `isFA='0'`) — users hit a dead-end "Try rephrasing!" message. Additionally:

- **No team context** — "best players on my team" doesn't work
- **No conversation memory** — follow-up questions have no context
- **Haiku for SQL gen** — lower accuracy on complex JOINs across players + abilities tables
- **No `validate_sql()` checks** — common pitfalls (missing CAST, fullName misuse) aren't caught

AskTSLModal has had self-correction + validation since v3.6.0. PlayerScout needs parity.

---

## Changes

### 1. SQL Generation Upgrade (oracle_cog.py ~line 3338)

**Before:** `tier=Tier.HAIKU`
**After:** `tier=Tier.SONNET`

Rationale: Sonnet produces more accurate SQL on first attempt for complex queries involving JOINs across players + player_abilities. Cost increase is negligible at ~31 users.

### 2. Caller Team Context (oracle_cog.py — new code after line 3298)

Resolve the caller's team and inject it into the scout prompt:

```python
# Resolve caller's team
caller_db = None
team_name = None
if _resolve_db_username_fn:
    caller_db = _resolve_db_username_fn(interaction.user.id)
if caller_db and not dm.df_teams.empty:
    mask = dm.df_teams["userName"].str.lower() == caller_db.lower()
    if mask.any():
        team_name = dm.df_teams[mask].iloc[0].get("nickName", "")

# Inject into scout_prompt (after schema, before question):
# "The user owns the {team_name}. When they say 'my team', 'my players',
#  or 'my roster', filter by teamName='{team_name}'."
```

**Column mapping:** `dm.df_teams["userName"]` matches the API username (same as `caller_db`). `nickName` (e.g., "Ravens") matches `players.teamName` values in the DB.

**Functions to reuse:**
- `_resolve_db_username_fn` (already imported in oracle_cog.py, line ~205)
- `dm.df_teams` DataFrame (already used in StrategyRoomModal)

**Graceful fallback:** If caller can't be resolved or team lookup fails, `team_name` stays `None` — the team context line is simply omitted from the prompt.

### 3. Self-Correction Loop (oracle_cog.py — replace lines 3348-3353)

Enhance beyond AskTSLModal's pattern (lines 3164-3180) by adding `validate_sql()` hints — AskTSL's self-correction only includes the error and schema, but we add targeted validation warnings for better fix accuracy:

```python
self_corrected = False
rows, error = run_sql(sql)
if error:
    # Validate for targeted hints
    warnings = validate_sql(sql)
    hint_block = "\n".join(f"- {w}" for w in warnings) if warnings else ""

    fix_prompt = (
        f"This SQLite query for Madden player data failed:\n{sql}\n\n"
        f"Error: {error}\n\n"
        f"REMINDER: ALL columns are TEXT. Use CAST(col AS INTEGER) for math.\n"
        f"{f'Validation warnings:\n{hint_block}\n' if hint_block else ''}"
        f"Fix the query. Return ONLY valid SQLite SQL.\n\n"
        f"Schema:\n{scout_schema}"
    )
    fix_result = await atlas_ai.generate(
        fix_prompt, tier=Tier.HAIKU, max_tokens=500, temperature=0.02
    )
    sql = extract_sql(fix_result.text) or sql
    rows, error = run_sql(sql)
    if not error:
        self_corrected = True
    if error:
        await interaction.followup.send(
            "⚠️ Scout query failed after retry. Try rephrasing!",
            ephemeral=True,
        )
        return
```

**Functions to reuse:**
- `validate_sql()` from codex_cog — **requires new import** (not currently imported in oracle_cog.py; add to the `try` block at line ~210 alongside existing codex_cog imports, with `validate_sql = None` default near line ~194)
- `extract_sql()` from codex_cog (already imported)
- `atlas_ai.generate()` with `Tier.HAIKU` for the fix (cheap, has error + schema context)

### 4. Conversation Memory Integration (oracle_cog.py — new code)

**Before SQL generation:** Fetch conversation context
```python
conv_block = ""
if _build_conversation_block:
    conv_block = await _build_conversation_block(interaction.user.id, source="codex")
```

**Inject into scout prompt** (append after the question):
```
If conv_block:
    scout_prompt += f"\nRECENT CONTEXT:\n{conv_block}\n"
```

**After answer generation:** Store the turn
```python
if _add_conversation_turn:
    await _add_conversation_turn(
        interaction.user.id, q, answer, sql=sql or "", source="codex"
    )
```

Using `source="codex"` means PlayerScout and AskTSL share conversation history — a follow-up in AskTSL can reference a PlayerScout query and vice versa. This partially addresses C7 (Cross-Modal Memory).

### 5. Enhanced Footer (oracle_cog.py ~lines 3386-3389)

**Before:**
```python
text=f"🔍 {len(rows)} players analyzed · ATLAS™ Oracle · Scout Mode"
```

**After:**
```python
footer_parts = [f"🔍 {len(rows)} players analyzed"]
if team_name:
    footer_parts.append(f"🏈 {team_name}")
if self_corrected:
    footer_parts.append("⚠️ Self-corrected")
footer_parts.append("ATLAS™ Oracle · Scout Mode")
embed.set_footer(text=" · ".join(footer_parts), icon_url=ATLAS_ICON_URL)
```

---

## Files Modified

| File | Changes |
|------|---------|
| `oracle_cog.py` | PlayerScoutModal.on_submit() — all 5 sections above (~50-70 net new lines) |
| `bot.py` | Bump `ATLAS_VERSION` patch (3.6.0 → 3.6.1) |

No new files created.

---

## Functions Reused (Not Reimplemented)

| Function | Source | Purpose |
|----------|--------|---------|
| `validate_sql()` | codex_cog.py:447 | SQL pitfall detection (**new import required**) |
| `extract_sql()` | codex_cog.py:436 | Parse SQL from AI response |
| `run_sql()` | codex_cog.py:424 | Execute SQL safely |
| `atlas_ai.generate()` | atlas_ai.py | Centralized AI client |
| `_resolve_db_username_fn` | build_member_db.py:1296 | Discord ID → db_username |
| `_build_conversation_block` | conversation_memory.py | Fetch conversation context |
| `_add_conversation_turn` | conversation_memory.py | Store Q&A turn |
| `get_persona()` | echo_loader.py | Analytical persona for answers |
| `dm.df_teams` | data_manager.py | Team lookup DataFrame |

---

## Testing

### Manual Regression (via Discord)

1. **Basic query:** "Who is the fastest WR?" → should return sorted results with CAST
2. **Team context:** "Best players on my team" → should resolve caller's team
3. **Cross-table JOIN:** "Which X-Factor QBs have the highest OVR?" → players + player_abilities JOIN
4. **Self-correction trigger:** "Players with most cap hit" → may need CAST fix; should self-correct
5. **Conversation follow-up:** Ask "Who's the best CB?" then "Compare him to the top SS" → should use memory
6. **Unknown caller:** Test from a non-registered Discord account → should work without team context (graceful fallback)
7. **Edge case:** "fullName" in AI response → `validate_sql()` should catch and hint

### What Success Looks Like

- Dead-end "Try rephrasing!" drops by >50% (self-correction catches most failures)
- "My team" queries work for registered members
- Follow-up questions reference previous Scout answers
- Footer shows team name and self-correction indicator when relevant

---

## Priority Order (Full V4 Roadmap)

| # | Item | Status |
|---|------|--------|
| **1** | **PlayerScout Upgrade** | **This spec** |
| 2 | Unified Modal Base (was C1) | Next |
| 3 | Multi-Retry SQL (was C3) | Pending |
| 4 | StrategyRoom Enrichment (was C6) | Pending |
| 5 | Cross-Modal Memory (was C7) | Partially addressed by #1 |
| 6 | Query Caching (was C2) | Pending |
| 7 | Result Citation (was C4) | Pending |

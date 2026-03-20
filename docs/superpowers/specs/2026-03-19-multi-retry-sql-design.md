# Multi-Retry SQL with Progressive Prompting — Design Spec

> **Priority:** #3 of 7 Oracle V4 improvements (was C3 in V4 handoff)
> **Date:** 2026-03-19
> **Scope:** codex_cog.py (new `retry_sql()`), oracle_cog.py (2 modal callers), bot.py (version bump)
> **Approach:** Extract shared retry function with 3-attempt cascade escalating through AI tiers

---

## Problem

All three SQL pipelines (AskTSLModal, PlayerScoutModal, Codex `/ask`) use a single-retry pattern: Sonnet generates SQL → if execution fails, Haiku attempts a fix with the error message → if that fails, the user sees "Try rephrasing!" This has two issues:

1. **One retry isn't enough.** Complex failures (wrong JOINs, missing CAST chains, schema misunderstandings) often need more than one correction attempt. The fix model sees only the error — not the original intent or its own previous attempt.

2. **Inconsistent retry quality.** PlayerScoutModal includes `validate_sql()` hints and tracks `self_corrected` in the footer. AskTSLModal has neither. The retry logic is duplicated inline across three locations with varying quality.

---

## Design

### Shared Function: `retry_sql()`

Placed in `codex_cog.py` alongside `run_sql()`, `validate_sql()`, and `extract_sql()`.

```python
async def retry_sql(
    sql: str,
    schema: str,
    *,
    params: tuple = (),
) -> tuple[list[dict], str, str | None, int, list[str]]:
    """Execute SQL with 3-attempt progressive retry cascade.

    Attempt 1: Execute as-is.
    Attempt 2: Haiku + error + validate_sql() hints + full schema.
    Attempt 3: Opus + both previous attempts + "think step by step".

    params are only used for Attempt 1. AI-regenerated SQL in Attempts
    2/3 is executed without params since the AI produces literal values.

    Returns:
        (rows, final_sql, error, attempt_num, warnings)
        - rows: query results (capped at MAX_ROWS)
        - final_sql: the SQL that ultimately ran (may differ from input)
        - error: None on success, error string if all 3 attempts failed
        - attempt_num: 1 = first try, 2 = Haiku self-correct, 3 = Opus rescue
        - warnings: validate_sql() output (empty list if not used)
    """
```

### Three-Attempt Cascade

**Attempt 1:** Execute `sql` via `run_sql(sql, params)`. If success → return `(rows, sql, None, 1, [])`.

**Attempt 2 (on failure):** Run `validate_sql(sql)` for targeted hints. Build fix prompt with error + hints + schema. Generate fix via `atlas_ai.generate()` at `Tier.HAIKU`, temp 0.02, max_tokens 500. Extract SQL, execute. If success → return with `attempt_num=2`. Haiku is used here (not Sonnet) to stay consistent with the recent cost optimization (commit `c237181`). The error message + validation hints give Haiku enough context for most fixable failures.

**Attempt 3 (on failure):** Build escalation prompt with both previous SQL attempts and both errors. Generate fix via `atlas_ai.generate()` at `Tier.OPUS`, temp 0.02, max_tokens 800. The prompt includes "think step by step" to force structured reasoning before SQL generation. Extract SQL, execute. If success → return with `attempt_num=3`. If failure → return with error from attempt 3.

### Prompt Templates

**Attempt 2:**
```
This SQLite query failed:
{sql_1}

Error: {error_1}

REMINDER: ALL columns are TEXT. Use CAST(col AS INTEGER) for math.
{f"Validation warnings:\n{hint_block}\n" if hints else ""}
Fix the query. Return ONLY valid SQLite SQL.

Schema:
{schema}
```

**Attempt 3:**
```
Two SQL attempts against this schema both failed.

Attempt 1:
{sql_1}
Error: {error_1}

Attempt 2:
{sql_2}
Error: {error_2}

{f"Validation warnings:\n{hint_block}\n" if hints else ""}
Think step by step: which tables and columns are needed,
what JOINs are required, and what CAST operations are necessary.
Then write the corrected SQL. Return ONLY valid SQLite SQL.

Schema:
{schema}
```

### Implementation Detail

```python
async def retry_sql(
    sql: str,
    schema: str,
    *,
    params: tuple = (),
) -> tuple[list[dict], str, str | None, int, list[str]]:
    # ── Attempt 1 ─────────────────────────────────────────
    rows, error_1 = run_sql(sql, params)
    if not error_1:
        return rows, sql, None, 1, []

    sql_1 = sql  # preserve original for attempt 3

    # ── Attempt 2: Haiku + validation hints ─────────────
    warnings = validate_sql(sql)
    hint_block = "\n".join(f"- {w}" for w in warnings) if warnings else ""

    fix_prompt_2 = (
        f"This SQLite query failed:\n{sql_1}\n\n"
        f"Error: {error_1}\n\n"
        f"REMINDER: ALL columns are TEXT. Use CAST(col AS INTEGER) for math.\n"
        f"{f'Validation warnings:\n{hint_block}\n' if hint_block else ''}"
        f"Fix the query. Return ONLY valid SQLite SQL.\n\n"
        f"Schema:\n{schema}"
    )
    fix_result_2 = await atlas_ai.generate(
        fix_prompt_2, tier=Tier.HAIKU, max_tokens=500, temperature=0.02
    )
    sql_2 = extract_sql(fix_result_2.text)
    if not sql_2:
        print("[retry_sql] Attempt 2: extract_sql returned None, reusing previous SQL")
        sql_2 = sql_1
    rows, error_2 = run_sql(sql_2)
    if not error_2:
        return rows, sql_2, None, 2, warnings

    # ── Attempt 3: Opus + full history + reasoning ────────
    fix_prompt_3 = (
        f"Two SQL attempts against this schema both failed.\n\n"
        f"Attempt 1:\n{sql_1}\nError: {error_1}\n\n"
        f"Attempt 2:\n{sql_2}\nError: {error_2}\n\n"
        f"{f'Validation warnings:\n{hint_block}\n' if hint_block else ''}"
        f"Think step by step: which tables and columns are needed, "
        f"what JOINs are required, and what CAST operations are necessary. "
        f"Then write the corrected SQL. Return ONLY valid SQLite SQL.\n\n"
        f"Schema:\n{schema}"
    )
    fix_result_3 = await atlas_ai.generate(
        fix_prompt_3, tier=Tier.OPUS, max_tokens=800, temperature=0.02
    )
    sql_3 = extract_sql(fix_result_3.text)
    if not sql_3:
        print("[retry_sql] Attempt 3: extract_sql returned None, reusing previous SQL")
        sql_3 = sql_2
    rows, error_3 = run_sql(sql_3)
    if not error_3:
        return rows, sql_3, None, 3, warnings

    return [], sql_3, error_3, 3, warnings
```

---

## Caller Conversions

### AskTSLModal._generate() (oracle_cog.py ~lines 3211-3228)

**Before:** Inline single retry with Haiku, no `validate_sql()`, no attempt tracking.
```python
rows, error = run_sql(sql)
if error:
    fix_prompt = (...)
    fix_result = await atlas_ai.generate(fix_prompt, tier=Tier.HAIKU, ...)
    sql = extract_sql(fix_result.text) or sql
    rows, error = run_sql(sql)
if error:
    await interaction.followup.send("📊 Couldn't generate...", ephemeral=True)
    raise _EarlyReturn()
```

**After:**
```python
schema = _build_schema_fn() if _build_schema_fn else ""
rows, sql, error, attempt, warnings = await retry_sql(sql, schema)
if error:
    await interaction.followup.send(
        "📊 Couldn't generate a query for that. Try rephrasing — "
        "be specific about player names, seasons, or owners.",
        ephemeral=True,
    )
    raise _EarlyReturn()
```

**Footer upgrade:** Add attempt indicator to existing footer format (uses `" | "` separator).
```python
if attempt > 1:
    footer_parts.append("⚠️ Self-corrected" if attempt == 2 else "🧠 Opus rescue")
```

### PlayerScoutModal._generate() (oracle_cog.py ~lines 3398-3419)

**Before:** Inline single retry with Haiku + `validate_sql()` hints + `self_corrected` flag.

**After:**
```python
rows, sql, error, attempt, warnings = await retry_sql(sql, scout_schema)
if error:
    await interaction.followup.send(
        "⚠️ Scout query failed after retry. Try rephrasing!",
        ephemeral=True,
    )
    raise _EarlyReturn()
```

**Footer upgrade:** Replace `self_corrected` boolean with `attempt` number.
```python
if attempt == 2:
    footer_parts.append("⚠️ Self-corrected")
elif attempt == 3:
    footer_parts.append("🧠 Opus rescue")
```

### Codex `/ask` pipeline (codex_cog.py ~lines 732-763)

The Codex flow currently calls `validate_sql()` pre-execution (line 733), then `run_sql()` (line 735), then an inline Haiku retry (lines 736-763). Since `retry_sql()` runs `validate_sql()` internally after failure, the pre-execution call is no longer needed — `retry_sql()` subsumes both validation and retry.

**Before:**
```python
sql_warnings = validate_sql(sql)
rows, error = run_sql(sql)
if error:
    # inline Haiku retry with validation hints ...
```

**After:**
```python
schema = _get_db_schema()
rows, sql, error, attempt, warnings = await retry_sql(sql, schema)
if error:
    await interaction.followup.send(
        "⚠️ ATLAS couldn't find an answer for that query. Try rephrasing:\n"
        "• Use full player names ('Patrick Mahomes' not 'Mahomes')\n"
        "• Specify the season ('in season 95' not 'this year')\n"
        "• Ask about one thing at a time\n"
        "• Use `/ask_debug` for technical details"
    )
    return
```

**Footer upgrade:** The Codex pipeline has an embed footer (line ~783, uses `" | "` separator). Add attempt indicator:
```python
if attempt > 1:
    footer_parts.append("⚠️ Self-corrected" if attempt == 2 else "🧠 Opus rescue")
```

---

## Import Changes

### oracle_cog.py (~line 209)

Add `retry_sql` to the existing codex_cog import block:
```python
from codex_cog import (
    ...
    retry_sql,
    ...
)
```

Add fallback default near line 197:
```python
retry_sql = None
```

If `retry_sql` is None (codex_cog import failed), callers fall back to a simple `run_sql()` with no retry — graceful degradation, same as `validate_sql`.

---

## Files Modified

| File | Changes |
|------|---------|
| `codex_cog.py` | Add `retry_sql()` function (~45 lines), update Codex pipeline to use it |
| `oracle_cog.py` | AskTSLModal + PlayerScoutModal: replace inline retry with `retry_sql()` call, add `retry_sql` import + fallback, update footers with attempt metadata |
| `bot.py` | Bump `ATLAS_VERSION` (3.7.0 → 3.8.0 — minor, new shared capability) |

No new files.

---

## Known Limitations

- **`run_sql()` is synchronous.** Each `run_sql()` call blocks the event loop briefly (~5ms for typical queries). In the worst case (all 3 attempts fail), this means 3 serial blocking calls. This is the same blocking pattern that exists today (just 2 calls max), and at ~31 users the risk of event loop starvation is negligible. Future work could wrap `run_sql()` in `run_in_executor()` if scale increases.

- **Cost: Opus on Attempt 3.** Opus is ~15x more expensive than Haiku per token. However, Attempt 3 only fires after two consecutive failures — expected volume is very low. At ~31 users with typical query patterns, this adds negligible cost.

---

## Implementation Notes

- `retry_sql()` relies on existing module-level imports in `codex_cog.py`: `atlas_ai`, `Tier`, `validate_sql`, `extract_sql`, `run_sql`. No new imports needed.
- `validate_sql` is always defined inside `codex_cog.py` (it's a module-level function), so no `if validate_sql` guard is needed. The guard is only needed in `oracle_cog.py` where the import may fail.
- Diagnostics use `print()` (consistent with the rest of `codex_cog.py` — no `logging` module is imported).

---

## What's NOT Changing

- **SQL generation** — `gemini_sql()` and PlayerScout's Sonnet prompt are untouched. `retry_sql()` only handles execution + retry, not initial generation.
- **`run_sql()`** — unchanged, still returns `(rows, error)`.
- **`validate_sql()`** — unchanged, still returns `list[str]`.
- **`extract_sql()`** — unchanged, still parses SQL from AI response.
- **Answer generation** — `gemini_answer()` still uses Haiku.
- **Non-AI modals** — H2H, TeamSearch, etc. don't use `retry_sql()`.

---

## Testing

### Verification

1. All 3 SQL pipelines still return correct results on first-attempt success (no regression)
2. Attempt 2 fires on SQL error and includes `validate_sql()` hints
3. Attempt 3 fires on attempt 2 failure and uses Opus with both previous attempts
4. Footer shows "⚠️ Self-corrected" for attempt 2, "🧠 Opus rescue" for attempt 3
5. If `retry_sql` import fails, callers degrade to simple `run_sql()` (no crash)
6. `params` argument is accepted but unused by current callers (reserved for future parameterized queries)

### How to Test

- Use Oracle Hub → AskTSL → "Who has the most wins?" (should succeed on attempt 1, no footer indicator)
- Use Oracle Hub → PlayerScout → "Players with most cap hit" (may trigger CAST fix → attempt 2 → "⚠️ Self-corrected")
- Intentionally malform a schema prompt to force all 3 attempts → verify Opus attempt fires and footer shows "🧠 Opus rescue"

---

## Priority Order (Full V4 Roadmap)

| # | Item | Status |
|---|------|--------|
| 1 | PlayerScout Upgrade | Done (v3.6.1) |
| 2 | Unified Modal Base | Done (v3.7.0) |
| **3** | **Multi-Retry SQL** | **This spec** |
| 4 | StrategyRoom Enrichment (was C6) | Pending |
| 5 | Cross-Modal Memory (was C7) | Partially addressed by #1 |
| 6 | Query Caching (was C2) | Pending |
| 7 | Result Citation (was C4) | Pending |

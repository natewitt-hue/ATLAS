# Multi-Retry SQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a shared `retry_sql()` function with a 3-attempt progressive cascade (execute → Haiku fix → Opus rescue), replacing inline single-retry patterns in all 3 SQL pipelines.

**Architecture:** New `retry_sql()` in `codex_cog.py` alongside existing `run_sql()`/`validate_sql()`/`extract_sql()`. Three callers adopt it: AskTSLModal, PlayerScoutModal (both in `oracle_cog.py`), and Codex `/ask` (in `codex_cog.py`). Each caller replaces its inline retry block with one `await retry_sql()` call and adds attempt metadata to its footer.

**Tech Stack:** Python 3.14, discord.py 2.3+, atlas_ai (Claude primary, Gemini fallback), SQLite

**Spec:** `docs/superpowers/specs/2026-03-19-multi-retry-sql-design.md`

---

### Task 1: Add `retry_sql()` function to codex_cog.py

**Files:**
- Modify: `codex_cog.py` — insert after `validate_sql()` (after line 493)

- [ ] **Step 1: Add the `retry_sql()` function**

Insert after `validate_sql()` (line 493), before `gemini_sql()` (line 496):

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
        print(f"[retry_sql] Attempt 2: extract_sql returned None, reusing previous SQL")
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
        print(f"[retry_sql] Attempt 3: extract_sql returned None, reusing previous SQL")
        sql_3 = sql_2
    rows, error_3 = run_sql(sql_3)
    if not error_3:
        return rows, sql_3, None, 3, warnings

    return [], sql_3, error_3, 3, warnings
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import codex_cog; print('retry_sql' in dir(codex_cog))"`
Expected: `True`

- [ ] **Step 3: Commit**

```bash
git add codex_cog.py
git commit -m "feat(codex): add retry_sql() 3-attempt progressive cascade"
```

---

### Task 2: Replace AskTSLModal inline retry with `retry_sql()`

**Files:**
- Modify: `oracle_cog.py` — import addition (line 200, line 212) + AskTSLModal._generate() (lines 3211-3228) + footer (lines 3236-3247)

- [ ] **Step 1: Add `retry_sql` import and fallback**

Add to the module-scope defaults (after line 200, alongside `validate_sql = None`):
```python
retry_sql = None
```

Add to the `from codex_cog import (...)` block (after line 212, alongside `validate_sql`):
```python
        retry_sql,
```

- [ ] **Step 2: Replace AskTSLModal inline retry (lines 3211-3228)**

Replace lines 3211-3228 (the `rows, error = run_sql(sql)` block through the `raise _EarlyReturn()`) with:

```python
            schema = _build_schema_fn() if _build_schema_fn else ""
            rows, sql, error, attempt, _warnings = await retry_sql(sql, schema)
            if error:
                await interaction.followup.send(
                    "⚠️ Couldn't pull that data. Try asking differently!", ephemeral=True
                )
                raise _EarlyReturn()
```

**Note:** If `retry_sql` is None (codex_cog import failed), `_HISTORY_OK` will be False and the base class guard blocks entry to `_generate()` entirely, so no fallback needed here.

- [ ] **Step 3: Add attempt indicator to AskTSL footer (after line 3236)**

The footer currently builds `footer_parts` starting at line 3236. After the existing entries (records analyzed, tier_label, alias_map, conv_block), add before the `return` at line 3244:

```python
        if attempt > 1:
            footer_parts.append("⚠️ Self-corrected" if attempt == 2 else "🧠 Opus rescue")
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import oracle_cog; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add oracle_cog.py
git commit -m "refactor(oracle): AskTSLModal adopts retry_sql() with attempt tracking"
```

---

### Task 3: Replace PlayerScoutModal inline retry with `retry_sql()`

**Files:**
- Modify: `oracle_cog.py` — PlayerScoutModal._generate() (lines 3398-3425) + footer (lines 3458-3463)

- [ ] **Step 1: Replace PlayerScoutModal inline retry (lines 3398-3425)**

Replace lines 3398-3425 (the `self_corrected = False` block through `raise _EarlyReturn()`) with:

```python
        # ── Execute with progressive retry ─────────────────────
        rows, sql, error, attempt, _warnings = await retry_sql(sql, scout_schema)
        if error:
            await interaction.followup.send(
                "⚠️ Scout query failed after retry. Try rephrasing!",
                ephemeral=True,
            )
            raise _EarlyReturn()
```

- [ ] **Step 2: Update PlayerScout footer (lines 3458-3463)**

Replace the `self_corrected` footer logic:
```python
        if self_corrected:
            footer_parts.append("⚠️ Self-corrected")
```

With attempt-based logic:
```python
        if attempt == 2:
            footer_parts.append("⚠️ Self-corrected")
        elif attempt == 3:
            footer_parts.append("🧠 Opus rescue")
```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import oracle_cog; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add oracle_cog.py
git commit -m "refactor(oracle): PlayerScoutModal adopts retry_sql() with Opus rescue"
```

---

### Task 4: Replace Codex `/ask` inline retry with `retry_sql()`

**Files:**
- Modify: `codex_cog.py` — Codex `/ask` pipeline (lines 732-763) + footer (lines 783-789)

- [ ] **Step 1: Replace Codex inline retry (lines 732-763)**

Replace lines 732-763 (from `sql_warnings = validate_sql(sql)` through the `return` after error message) with:

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

- [ ] **Step 2: Add attempt indicator to Codex footer (after line ~784)**

After `footer_parts.append("🧠 Tier 3 (NL→SQL)")` (line 784), add:

```python
            if attempt > 1:
                footer_parts.append("⚠️ Self-corrected" if attempt == 2 else "🧠 Opus rescue")
```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import codex_cog; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add codex_cog.py
git commit -m "refactor(codex): /ask pipeline adopts retry_sql() with attempt tracking"
```

---

### Task 5: Bump version and final verification

**Files:**
- Modify: `bot.py` — line 166 (ATLAS_VERSION)

- [ ] **Step 1: Bump version**

Change line 166:
```python
ATLAS_VERSION = "3.7.0"  # Unified modal base — _OracleIntelModal Template Method refactor
```
To:
```python
ATLAS_VERSION = "3.8.0"  # Multi-retry SQL — 3-attempt progressive cascade with Opus rescue
```

- [ ] **Step 2: Full import verification**

Run: `python -c "import bot; print(f'ATLAS v{bot.ATLAS_VERSION}')"`
Expected: `ATLAS v3.8.0`

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "feat: bump ATLAS_VERSION to 3.8.0 — multi-retry SQL"
```

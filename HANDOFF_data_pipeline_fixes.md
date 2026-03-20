# Handoff: Oracle & Data Pipeline — Code Fixes

**From:** GAP Review Instance 1 (deep read, no code changes)
**To:** Code Fix Session
**Date:** 2026-03-20
**Branch:** `claude/plan-atlas-instances-vc2CP`

---

## Context

A full line-by-line review of the Oracle & Data Pipeline was completed across 5 files (~8,760 lines total). The review found **3 bugs to fix** and **5 design risks to harden**. All MaddenStats API gotchas from CLAUDE.md are properly handled except where noted below. oracle_cog.py (4305 lines) and codex_cog.py (1163 lines) are clean — no changes needed.

**Files reviewed (no changes made):**
- `data_manager.py` — 1150 lines, MaddenStats API integration, DataFrames, caches
- `build_tsl_db.py` — 543 lines, SQLite sync from API CSV exports
- `build_member_db.py` — 1599 lines, identity registry, alias resolution
- `oracle_cog.py` — 4305 lines, stats hub, analytics, power rankings (CLEAN)
- `codex_cog.py` — 1163 lines, NL→SQL→NL pipeline (CLEAN)

---

## BUG-1: `get_weekly_results()` fallback drops status=2 games

**File:** `data_manager.py`
**Lines:** 800, 869

**Problem:** The primary path (line 836) correctly filters `status.isin([2, 3])`. The API fallback path (line 869) only checks `status != 3`, silently dropping status=2 (in-progress with final scores). This fallback fires when `df_all_games` is empty.

**What to change:**

1. Line 869 — change the status check:
```python
# BEFORE:
if int(g.get("status", 0)) != 3:  # final only
    continue

# AFTER:
if int(g.get("status", 0)) not in (2, 3):
    continue
```

2. Line 800 — fix the misleading docstring:
```python
# BEFORE:
"""
Return FINAL games only (status == 3) for the given week.

# AFTER:
"""
Return completed games (status 2 or 3) for the given week.
```

---

## BUG-2: `player_draft_map` non-deterministic teamName

**File:** `build_tsl_db.py`
**Lines:** 268-293

**Problem:** The subqueries at lines 286 and 290 use `GROUP BY rosterId` with a non-aggregated `teamName` column. SQLite picks an arbitrary row's `teamName` — not necessarily the one from the MIN season. This can credit draft picks to the wrong team.

**What to change:**

Replace the two LEFT JOIN subqueries (lines 285-292) with correlated subqueries that guarantee the teamName matches the MIN season:

```sql
-- BEFORE (line 285-288):
LEFT JOIN (
    SELECT rosterId, teamName, MIN(CAST(seasonIndex AS INTEGER)) AS seasonIndex
    FROM offensive_stats GROUP BY rosterId
) first_off ON p.rosterId = first_off.rosterId

-- AFTER:
LEFT JOIN (
    SELECT o.rosterId, o.teamName, CAST(o.seasonIndex AS INTEGER) AS seasonIndex
    FROM offensive_stats o
    INNER JOIN (
        SELECT rosterId, MIN(CAST(seasonIndex AS INTEGER)) AS min_season
        FROM offensive_stats GROUP BY rosterId
    ) m ON o.rosterId = m.rosterId AND CAST(o.seasonIndex AS INTEGER) = m.min_season
) first_off ON p.rosterId = first_off.rosterId
```

Apply the same pattern to the defensive_stats subquery at lines 289-292.

**Note:** If a player has multiple rows in the same MIN season (different weeks), this could return duplicates. Add `GROUP BY o.rosterId` to the outer subquery or use `LIMIT 1` per rosterId.

---

## BUG-3: `resolve_db_username()` connection leak

**File:** `build_member_db.py`
**Lines:** 1310-1393

**Problem:** `conn = sqlite3.connect(...)` at line 1310 has no `try/finally`. Multiple early `return` paths at lines 1321, 1328, 1347, 1389 call `conn.close()`, but any exception between open and close leaks the connection.

**What to change:**

Wrap the entire function body in `try/finally`:

```python
def resolve_db_username(discord_id: int | str, db_path: str = DB_PATH) -> str | None:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        # ... existing logic (lines 1311-1392) ...
        # Remove individual conn.close() calls from each return path
        # Just return the value; finally block handles close
    finally:
        conn.close()
```

There are 4 `conn.close()` calls to remove (lines ~1320, ~1327, ~1347, ~1392) and replace with a single `finally: conn.close()`.

---

## RISK-1: Magic number `stageIndex >= 200`

**File:** `data_manager.py`
**Line:** 292

**What to change:** Add a named constant and comment explaining the source:

```python
# MaddenStats stageIndex values: 1=Regular Season, 2=Wildcard, 3=Divisional,
# 4=Conference, 200+=Super Bowl. Championship games always have stageIndex >= 200.
_CHAMPIONSHIP_STAGE_MIN = 200
```

Then use `_CHAMPIONSHIP_STAGE_MIN` at line 292 instead of the literal `200`.

---

## RISK-2: `snapshot_week_stats()` may read nonexistent fields

**File:** `data_manager.py`
**Lines:** 1084-1097

**Problem:** `snapshot_week_stats()` reads `_players_cache` (from `/export/players` CSV) for fields like `passYds`, `rushYds`. These stat columns likely don't exist on the export CSV — they come from stat-leader endpoints. Every `.get(f, 0)` returns 0, making the blowout monitor a silent no-op.

**What to investigate:** Check what fields `/export/players` actually returns. If it doesn't include stat columns, the snapshot should read from `df_offense` and `df_defense` instead, or from `_players_cache` only if those fields exist.

**Minimum fix:** Add a guard that logs if the stat fields are missing:

```python
def snapshot_week_stats(week: int) -> None:
    if _players_cache and _PADDING_STAT_FIELDS[0] not in _players_cache[0]:
        log.warning("[dm] snapshot_week_stats: stat fields not in _players_cache — blowout monitor disabled")
        return
    # ... rest of function
```

---

## RISK-3: Missing `team_stats` table in sync path

**File:** `build_tsl_db.py`
**Lines:** 86-95, 369-373

**Problem:** `_CSV_EXPORTS` (line 86) doesn't include a `teamStats` endpoint, so `sync_tsl_db()` never creates the `team_stats` table. The manual `build_db()` path (line 513) loads `teamStats.csv` into it. Index creation at line 176 silently fails.

**What to investigate:** Does MaddenStats expose `/export/teamStats`? If yes, add it to `_CSV_EXPORTS`. If no, either:
- Remove the `team_stats` index from `_add_indexes()` (line 176), or
- Document that `team_stats` is only available via manual CSV import

---

## RISK-4: Codex SQL injection on retry attempts 2-3

**File:** `codex_cog.py`
**Lines:** 620-655

**Problem:** Attempt 1 uses parameterized queries (`run_sql(sql, params)`). Attempts 2 and 3 have the AI regenerate SQL with literal values baked in, then execute without parameterization. SELECT-only gate + sanitization mitigate but don't eliminate risk.

**What to change (hardening):** After extracting SQL in attempts 2-3, validate it contains no write operations:

```python
sql_upper = sql.strip().upper()
if not sql_upper.startswith("SELECT"):
    # AI generated non-SELECT — reject
    continue
```

This check already exists at line 500-501 in `run_sql()`, so this is already defended. Add a comment at lines 629/651 noting that `run_sql()` enforces SELECT-only as the injection backstop.

---

## RISK-5: 30+ members with no `discord_id`

**File:** `build_member_db.py`
**Lines:** Throughout MEMBERS array

**Problem:** Members without `discord_id` can't use ID-based resolution (`get_db_username_for_discord_id()`, `resolve_db_username()`). First-person `/ask` queries fail silently.

**What to change (opportunistic backfill):** In `codex_cog.py` `/ask` command, after successful resolution via `fuzzy_resolve_user()`, backfill the discord_id:

```python
# After line 801 in codex_cog.py:
if caller_db and not _get_db_username(interaction.user.id):
    # Opportunistically backfill discord_id for members resolved via alias
    try:
        member_db.upsert_member({
            "discord_id": str(interaction.user.id),
            "discord_username": interaction.user.name,
            "db_username": caller_db,
        })
    except Exception:
        pass
```

---

## Files NOT Requiring Changes

| File | Lines | Verdict |
|------|-------|---------|
| `oracle_cog.py` | 4305 | All 40+ SQL queries compliant. All 11 persona calls use `get_persona()`. All Discord constraints followed. No race conditions. No QUARANTINE imports. |
| `codex_cog.py` | 1163 | NL→SQL→NL pipeline robust. Schema includes dynamic `CURRENT_SEASON`. LRU cache sound. Identity resolution 4-tier cascade working. `validate_sql()` catches common mistakes. |

---

## Version Bump

Per CLAUDE.md: bump `ATLAS_VERSION` in `bot.py` before pushing. Current: `4.3.0`. These are bug fixes → patch bump to `4.3.1`.

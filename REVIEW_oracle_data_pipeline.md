# GAP Review: Oracle & Data Pipeline

**Reviewer:** Claude Code Instance 1
**Date:** 2026-03-20
**Scope:** `data_manager.py`, `build_tsl_db.py`, `build_member_db.py`, `oracle_cog.py`, `codex_cog.py`
**Verdict:** No critical bugs. 3 real bugs found, 5 design risks, several hardening opportunities.

---

## BUGS (should fix)

### BUG-1: `get_weekly_results()` fallback path drops status=2 games
**File:** `data_manager.py:869`
**Severity:** Medium — only fires when `df_all_games` is empty (rare)

The primary path correctly filters `status.isin([2, 3])` (line 836), but the API fallback path at line 869 only checks `status != 3`. In-progress games (status=2) with final scores are silently dropped in fallback mode.

```python
# Line 869 — CURRENT (wrong):
if int(g.get("status", 0)) != 3:  # final only
    continue

# FIX:
if int(g.get("status", 0)) not in (2, 3):
    continue
```

The docstring at line 800 also lies — says "status == 3" but the primary code path correctly uses 2 and 3. Docstring should be updated too.

---

### BUG-2: `player_draft_map` — non-deterministic `teamName` in GROUP BY
**File:** `build_tsl_db.py:286-291`
**Severity:** Medium — could credit drafts to wrong team

```sql
SELECT rosterId, teamName, MIN(CAST(seasonIndex AS INTEGER)) AS seasonIndex
FROM offensive_stats GROUP BY rosterId
```

SQLite returns an *arbitrary* `teamName` from the group, not necessarily the one matching the MIN season. The correct fix uses a correlated subquery or window function:

```sql
SELECT o.rosterId, o.teamName, o.seasonIndex
FROM offensive_stats o
INNER JOIN (
    SELECT rosterId, MIN(CAST(seasonIndex AS INTEGER)) AS min_season
    FROM offensive_stats GROUP BY rosterId
) m ON o.rosterId = m.rosterId AND CAST(o.seasonIndex AS INTEGER) = m.min_season
```

---

### BUG-3: `resolve_db_username()` — connection leak on exception
**File:** `build_member_db.py:1310-1393`
**Severity:** Low — leaks one SQLite connection per failed resolution

No `try/finally` wrapping the connection. If any DB operation raises between line 1310 (`conn = sqlite3.connect(...)`) and the various `conn.close()` calls, the connection leaks.

Fix: wrap in `try/finally` or use `with contextlib.closing(conn):`.

---

## DESIGN RISKS (worth tracking)

### RISK-1: `_rebuild_rings_cache()` — magic number `stageIndex >= 200`
**File:** `data_manager.py:292`

Undocumented assumption that championship games have stageIndex >= 200. No comment explains why. If MaddenStats changes stage numbering, rings silently break.

**Recommendation:** Add a comment documenting the source of this constant, or define `CHAMPIONSHIP_STAGE_MIN = 200` as a named constant.

---

### RISK-2: `snapshot_week_stats()` may snapshot zeros
**File:** `data_manager.py:1091-1097`

Reads `_players_cache` (from `/export/players` CSV) for stat fields like `passYds`, `rushYds`. But `/export/players` may not include these cumulative stat columns — they come from stat-leader endpoints. If the export CSV doesn't have these fields, every snapshot is zeros, and `flag_stat_padding()` never fires.

**Recommendation:** Verify which fields `/export/players` actually returns. If stats aren't included, snapshot should read from `df_offense`/`df_defense` instead.

---

### RISK-3: `sync_tsl_db()` doesn't create `team_stats` table
**File:** `build_tsl_db.py:369-373`

The sync path loads 7 tables but not `team_stats`. The manual `build_db()` path loads it from `teamStats.csv`. Index creation at line 176 tries `team_stats(seasonIndex)` and silently fails. Any query against `team_stats` after a sync (vs manual build) gets "no such table."

---

### RISK-4: SQL injection in Codex retry attempts 2-3
**File:** `codex_cog.py:620-655`

Attempt 1 uses parameterized queries. Attempts 2 and 3 have the AI regenerate SQL with literal values embedded, then execute without parameterization. The SELECT-only gate + input sanitization mitigate this, but it's not airtight.

**Recommendation:** Run all attempts through parameterized execution, or add a SQL AST parser to validate no side effects.

---

### RISK-5: 30+ members with no `discord_id`
**File:** `build_member_db.py` (throughout MEMBERS array)

These members can't use `get_db_username_for_discord_id()` or `resolve_db_username()`. First-person queries ("how am I doing?") will fail silently for them. They're only discoverable via `get_alias_map()`.

**Recommendation:** Backfill Discord IDs when members interact with the bot (e.g., capture `interaction.user.id` on first `/ask`).

---

## CLEAN (no issues found)

### oracle_cog.py (4305 lines) — All Clear
- **40+ SQL queries** — every one uses `status IN ('2','3')` ✅
- **CAST()** — used consistently for all TEXT→numeric comparisons ✅
- **weekIndex** — always adds +1 for display ✅
- **get_persona()** — used in all 11 AI prompt locations, never hardcoded ✅
- **No view=None** in any `followup.send()` call ✅
- **Select menus** — all capped at 25 options ✅
- **defer()** — called before every long operation ✅
- **No QUARANTINE imports** ✅
- **No direct API calls** — everything goes through `data_manager` ✅
- **No race conditions** — all state is per-interaction instance ✅
- **Error handling** — many `except Exception: pass` but all on optional features (AI blurbs, affinity, DNA bars). Acceptable pattern.

### codex_cog.py (1163 lines) — All Clear
- **NL→SQL→NL pipeline** — robust 3-tier architecture (intent fast-path → Sonnet SQL gen → Haiku answer) ✅
- **Schema dynamically includes `dm.CURRENT_SEASON`** — rebuilt every call ✅
- **LRU cache** — 200 entries, 5-min TTL, per-user keys, public invalidation API ✅
- **Identity resolution** — 4-tier cascade: dict → exact → fuzzy → substring, with AI fallback ✅
- **get_persona("analytical")** — used for all answer generation ✅
- **validate_sql()** — warns on missing status filter, missing CAST, wrong draft table ✅
- **3-attempt retry cascade** — raw → Haiku self-correct → Opus rescue ✅
- **Discord compliance** — all defer/followup patterns correct ✅

### build_tsl_db.py (543 lines) — Mostly Clean
- Atomic temp-file swap with Windows retry ✅
- Preserves non-API tables via ATTACH/DETACH ✅
- WAL mode enabled post-swap ✅
- `status IN ('2','3')` in all derived table queries ✅
- (BUG-2 noted above)

### build_member_db.py (1599 lines) — Mostly Clean
- Thread lock on table builds ✅
- BEGIN IMMEDIATE for write isolation ✅
- COALESCE preserves runtime team assignments ✅
- Orphan + ghost detection ✅
- `upsert_member()` vs `build_member_table()` COALESCE directions are intentionally different ✅
- (BUG-3 and RISK-5 noted above)

### data_manager.py (1150 lines) — Mostly Clean
- Atomic swap via staging locals ✅
- All caches rebuilt per-sync ✅
- Status filtering correct in primary paths ✅
- weekIndex off-by-one fixed and documented ✅
- (BUG-1, RISK-1, RISK-2 noted above)

---

## MaddenStats API Gotchas Compliance Scorecard

| Rule | Compliant | Evidence |
|------|-----------|---------|
| `/games/schedule` returns current week only | ✅ | `data_manager.py:39` comment, used correctly |
| `weekIndex` 0-based vs `CURRENT_WEEK` 1-based | ✅ | Fixed at line 70, documented, +1 in display |
| Completed games: `status IN ('2','3')` | ✅* | 40+ correct instances. *BUG-1 in fallback path |
| Full roster from `/export/players` | ✅ | `data_manager.py:565`, not stat-leader endpoints |
| Ability assignments from `/export/playerAbilities` | ✅ | `data_manager.py:602`, separate cache |
| `devTrait` mapping 0/1/2/3 | ✅ | Documented in codex schema, handled in data_manager |
| Draft credits original team | ✅* | `build_tsl_db.py:269-293`. *BUG-2 non-deterministic teamName |
| Owner resolution via fuzzy lookup | ✅ | 4-tier cascade in `build_member_db.py:1296-1393` |
| Ability budgets (Star=1B, SS=1A+1B, XF=1S+1A+1B) | N/A | Enforced in genesis_cog (Instance 3 scope) |
| Dual-attribute checks use OR logic | N/A | Enforced in genesis_cog (Instance 3 scope) |

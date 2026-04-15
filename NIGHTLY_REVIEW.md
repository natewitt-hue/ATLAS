# ATLAS Nightly Review — Oracle & Analytics Subsystem
**Date:** 2026-04-15
**Audit Task:** audit-wednesday-oracle
**Scope:** 7 focus files — oracle_cog.py, oracle_query_builder.py, analysis.py, intelligence.py, data_manager.py, oracle_agent.py, oracle_memory.py
**Passes:** Anti-pattern scan · Logic trace · Cross-module contract · Performance · Security/NL→SQL path

---

## Phase 1 — Recent Commit Triage

No changes to any Oracle focus file in the last 24 hours. Most recent commit (6644a83) was the Tuesday Casino nightly audit. Audit proceeds against current baseline.

---

## Phase 2 — Deep Audit Findings

### CRITICAL — 1 issue

---

#### C-01 · analysis.py L182 — team_profile() stores a coroutine object, never actual game data

**Severity:** CRITICAL — `recent` field always holds a dead coroutine object, never a list of games
**Files affected:** analysis.py:182, analysis.py:258–265 (transitive via head_to_head())

`team_profile()` is a synchronous function. At L182 it does:

```python
"recent": dm.get_last_n_games(team_name, 5),
```

`dm.get_last_n_games()` is declared `async def` in data_manager.py:814. Calling it without `await` in a sync context returns a coroutine object — never executes, never raises, and silently stores `<coroutine object get_last_n_games>` into the dict. Any consumer that iterates `result["recent"]` either gets nothing (truthiness check) or a TypeError.

`head_to_head()` at L258–265 calls `team_profile()` twice and surfaces both broken `recent` fields to callers, including Codex intent routing via `analyze_intent()` at L406–410.

**Fix:** Make `team_profile()` async and await the call:

```python
async def team_profile(team_name: str) -> dict:
    result = { ..., "recent": await dm.get_last_n_games(team_name, 5) }
```

---

### WARNINGS — 4 issues

---

#### W-01 · oracle_memory.py L458–472 + oracle_query_log — No automatic pruning on either table

`prune_old_turns(days=90)` exists but is never called automatically. No periodic task, no scheduled hook, no call from `load_all()`. Every `embed_and_store()` call adds a row + embedding BLOB forever.

`oracle_query_log` (observability table) has **no cleanup function at all** — every `log_query()` call writes a row with no TTL. Both tables grow without bound over a season of active use.

Compounding issue: after `prune_old_turns()` deletes rows from `conversation_memory`, the FTS5 virtual table shadow rows are orphaned until `INSERT INTO oracle_fts(oracle_fts) VALUES('optimize')` is run. FTS index accumulates dead segments across prune cycles (see O-02).

**Fix:** Add `@tasks.loop(hours=24)` periodic task to StatsHubCog calling `prune_old_turns(days=90)` and a new `prune_query_log(days=30)` method. Add FTS OPTIMIZE call inside `prune_old_turns()` after DELETE.

---

#### W-02 · intelligence.py — compare_draft_classes() N+1 sequential await loop (up to 93 iterations)

`compare_draft_classes()` awaits `get_draft_class(season)` sequentially for every season in a for loop:

```python
for season in range(2, dm.CURRENT_SEASON + 1):
    dc = await get_draft_class(season)   # sequential — blocks on each DB round-trip
```

With `CURRENT_SEASON` at 95+, this is up to 93 sequential `run_in_executor` DB calls before any response, dominating wall time for `/stats draft` without a season argument.

**Fix:** Parallelize with `asyncio.gather()`:

```python
seasons = range(2, dm.CURRENT_SEASON + 1)
results = await asyncio.gather(*[get_draft_class(s) for s in seasons], return_exceptions=True)
draft_classes = [r for r in results if isinstance(r, dict)]
```

---

#### W-03 · oracle_memory.py L399–455 — search_vector() O(n) cosine similarity in Python

`search_vector()` fetches up to 2000 rows with embedding BLOBs from SQLite and computes cosine similarity in a Python loop. No `discord_id` filter scopes the scan per user — every call scans the full table and degrades linearly as the table grows.

**Fix (short term):** Add `AND discord_id = ?` filter to scope per-user. Reduce LIMIT from 2000 to 500 — BM25 FTS handles recall; vector is a re-ranking pass, not primary retrieval.

**Fix (long term):** Add index on `(discord_id, created_at)` to the oracle_memory schema migration.

---

#### W-04 · data_manager.py L994–1025 — get_discord_db_schema() opens synchronous sqlite3.connect() on 1.3GB DB without executor

`get_discord_db_schema()` is called from async context (oracle_cog embed builders, Codex schema injection) but calls `sqlite3.connect(_DB_PATH)` synchronously with no `asyncio.to_thread()` wrapper.

5-minute TTL limits frequency, but a cache miss means a cold connection to a 1.3 GB database on the event loop thread, stalling all Discord interactions during bot cold start.

**Fix:**

```python
async def get_discord_db_schema() -> str:
    if _discord_schema_cache and (now - _discord_schema_ts) < _SCHEMA_TTL:
        return _discord_schema_cache
    return await asyncio.to_thread(_get_schema_sync)
```

---

### CROSS-MODULE RISKS — 2 issues

---

#### R-01 · oracle_cog.py L2377–2390 — _build_team_matchup_embed() labels current-season H2H as "All-Time H2H"

`_build_team_matchup_embed()` calls `dm.get_h2h_record(team_a, team_b)` and renders the result under field name **"📊 All-Time H2H"**. The `get_h2h_record()` docstring (data_manager.py:951–957) states:

> Head-to-head record between two teams for the **CURRENT SEASON only**. For all-time H2H, query tsl_history.db directly via Codex.

Users clicking the Matchup button believe they see career H2H history. In Week 1, every matchup shows 0–0.

**Fix:** Rename the embed field to "This Season H2H", or replace with a `run_sql()` all-time query against tsl_history.db across all seasons and stage indices.

---

#### R-02 · analysis.py:258–265 → oracle_cog.py / Codex — head_to_head() inherits C-01 coroutine bug for both teams

`head_to_head(team_a, team_b)` calls `team_profile()` twice. Both return dicts with `recent=<coroutine>`. Any AI path that serializes `team_profile()` output (Codex NL→analysis, matchup prompts) includes the coroutine repr string instead of actual game data. AI-generated team summaries silently omit recent form for both teams.

**Blocked by:** C-01 fix resolves this entirely.

---

### OBSERVATIONS — 3 issues

---

#### O-01 · oracle_cog.py L2475 — Hardcoded "6 seasons of history" in _build_alltime_embed()

```python
description="6 seasons of history — regular season",
```

`dm.CURRENT_SEASON` is 95+. Stale by ~89 seasons. Footer at L2596 correctly uses `f"Seasons 1–{dm.CURRENT_SEASON}"`, making description and footer inconsistent within the same embed.

**Fix:** `description=f"Seasons 1–{dm.CURRENT_SEASON} — regular season"`

---

#### O-02 · oracle_memory.py — FTS5 index not optimized after prune_old_turns()

After `DELETE FROM conversation_memory WHERE ...`, FTS5 shadow tables retain orphaned document segments until `OPTIMIZE` is called. Over many prune cycles the FTS index accumulates dead segments, degrading `search_fts()` performance.

**Fix:** Add to `prune_old_turns()` after the DELETE commit:

```python
await db.execute("INSERT INTO oracle_fts(oracle_fts) VALUES('optimize')")
```

(Partially addressed by W-01 fix.)

---

#### O-03 · intelligence.py — _paginated_messages stale entry accumulation in low-traffic windows

`_prune_stale_pages()` is called inside `register_pagination()` only. In a low-traffic window stale entries accumulate until the next registration. Noted in AUDIT_REPORT_2026_03_15, still open.

**Fix:** Call `_prune_stale_pages()` in `get_pagination()` as well.

---

## Phase 3 — Security Audit (NL→SQL Path)

**oracle_agent.py sandbox integrity:** PASS
- `validate_sandbox_ast()` rejects dunder traversal at AST level before `exec()`
- `_SAFE_BUILTINS` whitelist excludes `__import__`, `open`, `eval`, `type`, `exec`
- `build_agent_env()` injects only QueryBuilder read functions + `run_sql` — no write/schema mutation surface
- `run_sql` routes through `codex_utils` which opens `tsl_history.db` read-only

**oracle_query_builder.py injection surface:** PASS
- All user input flows through `_sanitize_input()` stripping `'";\\-` before SQL inclusion
- `Query.__init__` validates against `_VALID_TABLES` frozenset; raises `ValueError` on unknown table
- `Query.build()` uses `?` parameterized placeholders for all filter values
- `game_extremes()` ORDER BY uses a 3-key dict whitelist, not user input directly
- Composite functions use f-strings on controlled internal stat definition strings from DomainKnowledge

**_franchise_nemesis() / _franchise_punching_bag():** PASS — `LIKE ?` parameterization; input filtered through `fuzzy_resolve_user()`

**oracle_memory.py FTS sanitization:** PASS — `_sanitize_fts()` strips FTS5 metacharacters before MATCH

**Overall NL→SQL verdict:** No injection path identified. Sandbox, parameterization, and whitelist layers correctly implemented.

---

## Phase 4 — CLAUDE.md Health Check

| Rule | Status |
|------|--------|
| `get_persona()` for all AI system prompts | PASS — `get_persona('analytical')`, `get_persona('casual')` throughout oracle_cog.py |
| `atlas_ai.generate()` for all AI calls | PASS — No direct Gemini/Claude SDK calls in focus files |
| `atlas_ai.is_available()` guard before AI calls | PASS — Used at L2441 and all AI injection points |
| `run_in_executor` for blocking DB calls | PARTIAL FAIL — `get_discord_db_schema()` missing executor wrap (W-04); all others correct |
| Dead files in QUARANTINE/ only | PASS — No imports from QUARANTINE found |
| `status IN ('2','3')` not `='3'` alone | PASS — All `games` table queries correctly use `status IN ('2','3')` |
| `weekIndex` 0-based vs `CURRENT_WEEK` 1-based | PASS — `week_index = target - 1` correctly applied in `get_weekly_results()` |
| `stageIndex='1'` for regular season | PASS — All `run_sql` calls on games table include `stageIndex='1'` |

---

## Summary

| Category | Count |
|----------|-------|
| Critical | 1 |
| Warnings | 4 |
| Cross-Module Risks | 2 |
| Observations | 3 |
| Security findings | 0 (PASS) |
| **Total** | **10** |

**Highest priority:** C-01 — `analysis.team_profile()` async bug. Silent data corruption where `recent` game form is never populated in any team profile or H2H analysis built through analysis.py. One-line fix once function is made `async def`.

**Second priority:** W-01 — `oracle_query_log` has no cleanup path. Will become the largest table in tsl_history.db with no drain mechanism as query volume grows.
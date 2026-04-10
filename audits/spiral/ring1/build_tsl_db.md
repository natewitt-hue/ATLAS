# Adversarial Review: build_tsl_db.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 547
**Reviewer:** Claude (delegated subagent)
**Total findings:** 14 (2 critical, 6 warnings, 6 observations)

## Summary

`sync_tsl_db()` is a mostly-sensible temp-file-then-swap rebuild, but several latent bugs make it unsafe under real failure modes: indexes on preserved tables (`conversation_history`, `tsl_members`, `server_config`) are silently dropped on every rebuild (perf regression), the connection leaks on exception paths (holds Windows file locks on the tmp file), the Windows swap fallback uses `shutil.copy2` without first closing open readers (brief corruption window), `PRAGMA synchronous=FULL` is applied *after* the commits it's meant to harden, and `balance_snapshots` is listed in the preserve allowlist but lives in a completely different database file (`flow_economy.db`). There is also no concurrency guard — `/god rebuilddb`, `/wittsync`, boss-cog refresh, and on_ready startup can all fight over the same `tmp_path` with no mutex. Downstream caches (`_invalidate_caches()`) are only cleared by the `bot.py` startup path, so admin-triggered rebuilds leave stale `codex_cog` query caches.

## Findings

### CRITICAL #1: Exception path leaks sqlite3 connection, holds Windows file lock on tmp DB

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:344-484`
**Confidence:** 0.92
**Risk:** If any step between `conn = sqlite3.connect(tmp_path)` (line 350) and `conn.close()` (line 425) raises — an API 5xx mid-rebuild, a bad CSV row triggering a `TypeError`, an index creation failure, a corrupted ATTACH DATABASE, etc. — control jumps to the `except` block on line 476. That block attempts `os.remove(DB_PATH + ".tmp")` on line 482 but never calls `conn.close()`. On Windows, sqlite3 holds an exclusive file lock on the DB file until the connection object is garbage collected OR explicitly closed. `os.remove` on a locked file raises `PermissionError`, which is silently swallowed by the inner `except Exception: pass`.
**Vulnerability:** The cleanup path does not close the connection before attempting file deletion. The tmp file is therefore retained on disk (potentially with a leaked file lock) until Python garbage collects the `conn` object (may be minutes later, or never in long-running Discord bots where the local `conn` variable falls out of scope but a reference lingers in a traceback object via `sys.exc_info`). The next rebuild's `os.remove(tmp_path)` on line 349 then hits a stale lock and raises, cascading into the outer try/except — rebuild continues anyway, but only after another logged failure.
**Impact:** Recurring rebuild failures after any transient error. Disk bloat from orphaned `tsl_history.db.tmp` files. Worst case on disk-full conditions, the retained tmp file prevents successful completion of subsequent rebuilds indefinitely.
**Fix:** Wrap `conn` acquisition in a `try/finally` that closes the connection regardless of exception:
```python
conn = None
try:
    conn = sqlite3.connect(tmp_path)
    # ... rebuild work ...
    conn.close()
    conn = None
    # ... swap ...
except Exception as e:
    ...
finally:
    if conn is not None:
        try: conn.close()
        except Exception: pass
    if os.path.exists(tmp_path):
        try: os.remove(tmp_path)
        except OSError: pass
```

### CRITICAL #2: Indexes on preserved tables are silently destroyed on every rebuild

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:399-420`
**Confidence:** 0.95
**Risk:** The preserve path on lines 410-416 copies the `CREATE TABLE` SQL from `old_db.sqlite_master` and re-executes it, then `INSERT INTO main.{tbl} SELECT * FROM old_db.{tbl}`. It does NOT query for `type='index'` rows, so every index on every preserved table is lost. `conversation_history` declares `idx_conv_user_time` and `idx_conv_chain` (per `conversation_memory.py:78-104`) lazily via `CREATE INDEX IF NOT EXISTS`, but that lazy-init is guarded by a module-level `_db_initialized` flag (`conversation_memory.py:56-63`). After a rebuild, the flag is still `True` in the process, so the indexes are never recreated until the bot restarts.
**Vulnerability:** Identity-registry reads via `build_member_db.get_db_username_for_discord_id()` and `tsl_members` lookups also lose whatever indexes they had declared. `server_config` reads on `config_key`/`guild_id` degrade to full scans. The degradation is silent — queries still return correct results, just slower, which is hard to detect without profiling.
**Impact:** Every `/wittsync` or `/rebuilddb` silently strips indexes off preserved tables. Over the course of a single session (multiple admin rebuilds), Oracle conversation lookups, identity resolution, and server_config queries all degrade to table scans. The degradation compounds because `_db_initialized = True` sticks until bot restart. In a multi-hour live-game session with >100k conversation_history rows, this turns O(log n) lookups into O(n) and can cause Oracle responses to time out.
**Fix:** Copy both tables AND their indexes from the old DB:
```python
for tbl in _PRESERVE_TABLES:
    if tbl not in old_tables:
        continue
    # Copy table schema + data
    schema_row = conn.execute(
        "SELECT sql FROM old_db.sqlite_master WHERE type='table' AND name=?",
        (tbl,)
    ).fetchone()
    if schema_row and schema_row[0]:
        conn.execute(schema_row[0])
        conn.execute(f'INSERT INTO main."{tbl}" SELECT * FROM old_db."{tbl}"')
        # Copy all indexes associated with this table
        idx_rows = conn.execute(
            "SELECT sql FROM old_db.sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (tbl,)
        ).fetchall()
        for (idx_sql,) in idx_rows:
            try:
                conn.execute(idx_sql)
            except sqlite3.OperationalError as e:
                log.warning("[TSL-DB] Could not recreate index for %s: %s", tbl, e)
```

### WARNING #1: No concurrency guard — parallel rebuilds race on tmp_path

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:310-498`
**Confidence:** 0.88
**Risk:** `sync_tsl_db()` is called from at least four places with no mutex or cooperative lock:
1. `bot.py:412` in `_startup_load()` (startup thread)
2. `god_cog.py:96` via `/god rebuilddb` (`run_in_executor`)
3. `boss_cog.py:2566` via Boss refresh (`run_in_executor`)
4. The manual CLI path via `build_db()`
If a user clicks `/god rebuilddb` while a scheduled reconnect triggers `_startup_load()`, or two admins click simultaneously, both calls race on `tsl_history.db.tmp`. The line 349 `os.remove(tmp_path)` deletes the other thread's in-progress tmp file; the line 350 `sqlite3.connect(tmp_path)` then reopens it. Two threads are now writing the same tmp file simultaneously. The final `os.replace(tmp_path, DB_PATH)` from whichever thread finishes last wins — and the "winner" may have interleaved writes from both threads, producing a corrupted SQLite database.
**Vulnerability:** No file-level advisory lock, no threading.Lock, no process-level mutex, and no "rebuild in progress" sentinel. The `_startup_done` flag in `bot.py` only prevents duplicate `_startup_load()` runs via the same process, not concurrent admin triggers.
**Impact:** Rare but catastrophic: SQLite file corruption from interleaved page writes. The atomic-swap design *assumes* single-writer, but nothing enforces it. Symptom: `sqlite3.DatabaseError: file is not a database` on the next read after a bad race.
**Fix:** Add a module-level `threading.Lock()` and acquire it at the top of `sync_tsl_db`:
```python
import threading
_rebuild_lock = threading.Lock()

def sync_tsl_db(players=None, abilities=None) -> dict:
    if not _rebuild_lock.acquire(blocking=False):
        return {"success": False, "errors": ["Rebuild already in progress"], ...}
    try:
        # ... existing body ...
    finally:
        _rebuild_lock.release()
```

### WARNING #2: `balance_snapshots` in preserve list is dead logic — table lives in flow_economy.db

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:395-398`
**Confidence:** 0.97
**Risk:** `_PRESERVE_TABLES` lists `"balance_snapshots"`, but every writer and reader of `balance_snapshots` targets `flow_economy.db` (per `db_migration_snapshots.py:27`, `flow_cards.py:28,60`, `sportsbook_cards.py:102,117`, `flow_sportsbook.py:3121`). No code ever writes `balance_snapshots` into `tsl_history.db`. The preserve loop on line 407 silently skips it (`if tbl in old_tables:` is always False), which masks the confusion.
**Vulnerability:** Someone reading this code will believe sparkline data persists across rebuilds — it does, but for the wrong reason (it's in a different DB file that this rebuild never touches). If a future engineer "fixes" the preserve list or mistakenly tries to move sparkline data into `tsl_history.db`, they will trip over the inconsistent reality.
**Impact:** Low immediate impact (the skip is silent), but a maintenance landmine. Also a smell: if the audit catches this, what else is wrong about the preserve allowlist?
**Fix:** Remove `"balance_snapshots"` from `_PRESERVE_TABLES`. Add an assertion or `log.info` at startup listing the actual preserved tables found for transparency.

### WARNING #3: `PRAGMA synchronous = FULL` applied after the commits it's meant to protect

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:249,299,423-424`
**Confidence:** 0.90
**Risk:** `_build_derived_tables()` calls `conn.commit()` on lines 249 and 299 (owner_tenure and player_draft_map) at the default sqlite3 synchronous level (NORMAL for journal mode, FULL for rollback journal). `_load_rows_into_table()` also calls `conn.commit()` on line 164 for every data table. Then line 423 finally sets `PRAGMA synchronous = FULL` followed by another `conn.commit()`. Per SQLite semantics, `PRAGMA synchronous` controls the sync level for *future* writes — it does NOT retroactively flush prior commits to disk.
**Vulnerability:** The comment on line 422 says "Ensure all data is flushed to disk before the atomic swap" — but the prior commits already happened at whatever level was in effect, which during temp-file rebuild (not WAL, no explicit journal_mode set) is the sqlite3 default NORMAL. The pragma is a no-op for the data that's already been written.
**Impact:** In the rare case of a power loss between the last `commit()` and `os.replace()`, the new DB may be missing recent writes that the rebuild thought it had persisted. Low probability, high cost (data inconsistency that looks like a "successful" rebuild per the log).
**Fix:** Set `PRAGMA synchronous = FULL` (or even EXTRA) immediately after opening the connection on line 350, BEFORE any writes, so all subsequent commits honor it:
```python
conn = sqlite3.connect(tmp_path)
conn.execute("PRAGMA synchronous = FULL")
conn.execute("PRAGMA journal_mode = DELETE")  # avoid WAL during build
```

### WARNING #4: Windows swap fallback `shutil.copy2` can corrupt readers mid-query

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:448-463`
**Confidence:** 0.78
**Risk:** The retry loop on line 449 catches `PermissionError` (file locked by another reader) up to 3 times with 1s sleeps. If all 3 fail, line 462 falls back to `shutil.copy2(tmp_path, DB_PATH)`, which on Windows opens `DB_PATH` for writing and overwrites its bytes in place. But readers (codex_cog, oracle_cog, etc.) may be mid-transaction on the old file via sqlite3 connections. Overwriting the bytes of a file held open by SQLite is undefined behavior — the reader may see a mix of old and new pages, or hit `SQLITE_CORRUPT`, or return garbage rows, or succeed accidentally.
**Vulnerability:** The atomic-swap design depends on `os.replace()` succeeding. The fallback is a "best-effort" that actively defeats the atomicity it was supposed to guarantee. The comment on line 461 ("Fallback: copy over + remove tmp") undersells the risk.
**Impact:** Data corruption or bogus query results visible to the user in the 100ms window during `shutil.copy2`. Rare but unreproducible — the kind of bug that will take days to track down.
**Fix:** If `os.replace()` fails 3x, escalate the failure rather than silently copying over a live file. Return `{"success": False, "errors": ["Could not acquire exclusive lock on tsl_history.db — other readers are active"]}` and let the caller retry later. The whole point of the atomic swap is to avoid corruption.

### WARNING #5: `_invalidate_caches()` not called from `/god rebuilddb` or boss-cog refresh path

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:465-475` (the callers in `god_cog.py:94-97` and `boss_cog.py:2562-2567`)
**Confidence:** 0.92
**Risk:** Per CLAUDE.md and `bot.py:215-221`, `_invalidate_caches()` clears the codex_cog query cache after a data refresh — "Called from all sync_tsl_db paths." But only `_startup_load()` actually calls it (bot.py:420). The god_cog and boss_cog admin commands invoke `sync_tsl_db()` without clearing the cache. After a successful rebuild via `/god rebuilddb`, codex_cog can still return stale cached query results based on the old DB state until the next bot restart.
**Vulnerability:** The rebuild function doesn't own cache invalidation, but also doesn't document or enforce that callers must clear downstream caches. The fix could also live in `sync_tsl_db()` itself via a callback, or be documented in the docstring so callers don't forget.
**Impact:** Admin-triggered rebuilds appear to "not take effect" — the admin sees "rebuild complete" but codex queries return old results. Support burden and confusion.
**Fix:** Either (a) add an optional `on_success: Callable[[], None]` parameter to `sync_tsl_db()` that runs after the atomic swap succeeds, or (b) add `from codex_cog import clear_query_cache; clear_query_cache()` inside `sync_tsl_db()` itself (with a try/except for the import). Option (a) is cleaner. Also update the `sync_tsl_db()` docstring to flag the requirement.

### WARNING #6: `_TABLE_SCHEMAS["trades"]` is too narrow for a real trades export

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:64-66,142-143`
**Confidence:** 0.72
**Risk:** When `/export/trades` returns zero rows (e.g., API temporarily down or no trades yet this season), `_load_rows_into_table` takes the empty-table branch on lines 134-143 and creates `trades` with the declared schema `(tradeId TEXT, seasonIndex TEXT, weekIndex TEXT)`. Any downstream reader that depends on columns the live API would have returned (e.g., `team1Id`, `team2Id`, player references) will then fail with `no such column`. `genesis_cog` and other consumers of `trades` data would break until the next successful rebuild.
**Vulnerability:** `_TABLE_SCHEMAS` is both load-bearing (empty fallback) and incomplete. It claims to be the minimal schema, but "minimal" here doesn't match what downstream code needs. The non-empty path on lines 153-163 derives the schema from `rows[0].keys()`, so the declared `_TABLE_SCHEMAS` entries only matter in empty-path recovery — and they're out of sync with the real API shape.
**Impact:** First rebuild after API downtime with no trades yet: `trades` table has 3 columns, downstream queries fail. Self-healing on next rebuild when trades exist, but unclear user-facing errors in the interim.
**Fix:** Either (a) preserve the old `trades` schema from the prior DB when the API returns no rows (delete-then-copy from `old_db.trades` where status matches) or (b) expand `_TABLE_SCHEMAS["trades"]` to match the real API contract. Option (a) is more robust because the API contract drifts.

### OBSERVATION #1: F-string interpolation of `tbl` variable in preserve path despite allowlist

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:416`
**Confidence:** 0.65
**Risk:** `conn.execute(f"INSERT INTO main.{tbl} SELECT * FROM old_db.{tbl}")` f-strings the table name into DDL-ish SQL. Today this is safe because `tbl` comes from the hardcoded `_PRESERVE_TABLES` list. But there's no quoting (unlike lines 139-142 which use double-quoted identifiers) and no comment warning future maintainers. If someone later populates `_PRESERVE_TABLES` dynamically or from config, this becomes an injection sink.
**Vulnerability:** Bad pattern that works today by happy coincidence of hardcoded input.
**Impact:** None today. Latent footgun.
**Fix:** Use quoted identifiers consistently: `conn.execute(f'INSERT INTO main."{tbl}" SELECT * FROM old_db."{tbl}"')` and add a comment matching the one on lines 131-133 about the allowlist invariant.

### OBSERVATION #2: `player_draft_map.drafting_team` misattributes when rookie traded before first game

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:273-298`
**Confidence:** 0.70
**Risk:** The derived `drafting_team` uses `COALESCE(first_off.teamName, first_def.teamName, p.teamName)` — the team on the rookie's earliest statistical appearance. Per CLAUDE.md's MaddenStats gotcha ("Credit players to the team that drafted them, first statistical appearance"), this matches the documented convention. But if a rookie is drafted and then traded before recording any offensive or defensive stat, both subqueries return NULL and the fallback `p.teamName` assigns the player's *current* team as the drafter — which is wrong. Rare (most rookies appear in preseason snaps), but possible for deep backups or IR-stashed rookies.
**Vulnerability:** The "first statistical appearance" heuristic has no correctness fallback for stat-less rookies.
**Impact:** Off-by-one or off-by-team in draft history queries. Low frequency, but if a user's favorite backup QB was drafted and immediately traded, Codex "who drafted X" queries will lie.
**Fix:** If both `first_off` and `first_def` are NULL, fall back to `trades` history or `draftRound/draftPick` + original draft season data rather than `p.teamName`. Best effort is still `p.teamName`, but log it as an unresolved case so Sentinel can flag it.

### OBSERVATION #3: `print()` instead of `log.info()` throughout

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:117,251,301,336,340,342,471,538,543`
**Confidence:** 0.95
**Risk:** The file uses `log = logging.getLogger(__name__)` correctly for warnings (lines 108, 112, 120, 122, 191, 420, 445, 478) but uses `print()` for success messages and progress output. Inconsistent — in a bot that may be run under systemd or as a Windows service, `print()` goes to stdout which may not be captured by whatever log aggregator is wired up. Also defeats structured log filtering and log-level gating.
**Vulnerability:** Observability gap. Can't filter rebuild progress logs by severity; can't tail structured logs to see rebuild status.
**Impact:** Operations pain. Debugging a production rebuild requires capturing raw stdout.
**Fix:** Replace all `print()` calls in this file with `log.info(...)` and configure the root logger in `bot.py` to format at INFO level.

### OBSERVATION #4: `_add_indexes` silently skips derived indexes whose target tables don't exist in API sync

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:181-182,188-191`
**Confidence:** 0.80
**Risk:** The `team_stats` table is listed in `file_map` for manual CSV build (line 518) but is NOT in `_CSV_EXPORTS` for the API sync (lines 86-95). On the API-sync path, `team_stats` never exists — so `idx_ts_season` and `idx_ts_team` attempt to `CREATE INDEX ... ON team_stats(...)` and fail with "no such table". The try/except on lines 188-191 swallows the error as `log.debug`, so it's invisible at default log levels.
**Vulnerability:** Dead index attempts in the hot path, plus a divergence between manual-CSV and API-sync schemas hidden behind a silent debug log.
**Impact:** Minor — harmless at runtime but confusing in the logs and suggests the file's two build paths have drifted.
**Fix:** Split `_add_indexes` into two: one for tables that always exist (owner_tenure, player_draft_map, games, teams, players, stats) and one for CSV-only tables (team_stats). Call the CSV-only path only from `build_db()`.

### OBSERVATION #5: `_transform_game` silently zeros scores on parse failure — ties

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:194-214`
**Confidence:** 0.80
**Risk:** When `homeScore` or `awayScore` can't be parsed to int, both are set to 0 (lines 199-200). Since `home == away`, the transform then sets all winner/loser fields to None (line 213) — marking the game as a tie. But a parse failure is not the same as a tie. Downstream aggregations over `winner_user` will miscount — a garbage-data game becomes a "tie" in the standings.
**Vulnerability:** Silent data coercion masquerading as a valid tie.
**Impact:** If the MaddenStats API ever returns a malformed score row (e.g., `"--"` during a live game in progress, or empty string for a scheduled future game), it becomes a recorded tie in `owner_tenure` counts. Low probability if the API is clean, but invisible if it happens.
**Fix:** Distinguish parse failure from a true tie. Set winner/loser to None only when `home == away` AND both parsed successfully. On parse failure, skip the game entirely or set a `parse_error` column. Also filter by `status IN ('2','3')` in the transform so scheduled/in-progress games don't get tie-tagged.

### OBSERVATION #6: Shared mutable state from `dm.get_players()` — no defensive copy

**Location:** `C:/Users/natew/Desktop/discord_bot/build_tsl_db.py:310-367`
**Confidence:** 0.68
**Risk:** `sync_tsl_db(players=dm.get_players(), ...)` passes `_state._players_cache` by reference (per `data_manager.py:741-743`). If `dm.load_all()` or another subsystem mutates `_players_cache` concurrently — e.g., background refresh, another cog's player update — the rebuild iterates a list that's changing under it, which can raise `RuntimeError: dictionary changed size during iteration` inside the sqlite executemany.
**Vulnerability:** Assumption of exclusive ownership of the passed list. The parameter type hint is `list | None` with no mention of "must be a snapshot." On Windows, when `data_manager` runs its reload in the same event loop thread during a subsequent API refresh, the risk drops, but nothing enforces thread-exclusivity.
**Impact:** Rare `RuntimeError` mid-rebuild, caught by the outer `except` and logged as "Fatal: dictionary changed size during iteration". Rebuild fails, retries on next `/wittsync`.
**Fix:** Snapshot-copy the input lists at the top of `sync_tsl_db`:
```python
if players is not None:
    players = [dict(p) for p in players]
if abilities is not None:
    abilities = [dict(a) for a in abilities]
```

## Cross-cutting Notes

**Rebuild concurrency** is a subsystem-wide concern, not just this file. Any code path that writes to `tsl_history.db` (this file, `build_member_db.py`, `setup_cog.py`, `conversation_memory.py`, `oracle_memory.py`) is vulnerable to racing with `sync_tsl_db`'s atomic swap. The right solution is a single process-wide rebuild lock in `bot.py` that all write paths respect, not a per-file lock. Consider adding a `ATLAS_DB_REBUILD_LOCK` module at the ring-0 level that every sync_tsl_db caller and every `tsl_history.db` writer acquires.

**Index preservation** (CRITICAL #2) likely affects `build_member_db.py` as well — if that file declares any indexes on `tsl_members`, they're also being dropped on every rebuild. Worth auditing the whole "preserve non-API tables" contract holistically: the set of tables, their indexes, their triggers, their views. Right now the contract is implicit and incomplete.

**Observability** of the rebuild is weak across multiple findings (print vs log, silent debug-level index failures, silent empty-table fallback). The rebuild is a critical hot path that runs on every startup and every admin click — it deserves structured logging at INFO level for every step so operators can see what actually happened.

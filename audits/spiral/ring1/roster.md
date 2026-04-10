# Adversarial Review: roster.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 498
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (2 critical, 7 warnings, 8 observations)

## Summary

`roster.py` is the single source of truth for Discord-user ↔ team assignments, and it has multiple material weaknesses: a genuine TOCTOU race on `assign()` that allows two concurrent commissioner actions to orphan a team or strand a member, synchronous sqlite in async contexts that blocks the event loop under load, and no transactional wrapper around the two-step "clear holder + set new holder" write. There is also no exception handling around any DB call — a corrupt or locked `tsl_history.db` takes down `bot.py` startup, `/boss` assignment flows, and any caller of `load()`. The read API is solid; the write API and UI wrapper are the risky surfaces.

## Findings

### CRITICAL #1: TOCTOU race in `assign()` allows orphaned / stolen teams under concurrent commissioner actions
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:249-283`
**Confidence:** 0.85
**Risk:** Two commissioners (or one commissioner double-clicking a select menu on slow network) performing `assign()` concurrently can produce:
  - Member A claims CHI; at the same instant member B claims CHI from a separate interaction.
  - Thread 1 runs `UPDATE ... team=NULL WHERE team='CHI' AND discord_id != A` (clears nothing, A is new).
  - Thread 2 runs the same statement and clears A's row (A just set it? no — both are setting, but interleavings of the two-statement sequence produce: A set, B clears A, B set ⇒ only B holds CHI, which is correct. However, a worse interleaving: A1-clear, B1-clear, A2-set(A,CHI), B2-set(B,CHI) ⇒ ONE row wins on the unique logical constraint but both are set ⇒ B overwrites A silently with no notification to A and no audit trail.
  - Additionally: if both A and B previously held *different* teams, step 1 does NOT unassign the caller from their old team. `UPDATE tsl_members SET team = NULL WHERE team = ? AND discord_id != ?` only clears the *target* team's other holder. The caller's prior team is stranded — they are now assigned to the new team but their old team row is never cleared, so `_by_team[OLD]` still points to them after reload. End result: one user holds two `_by_team` slots via divergent rows (impossible in the atomic swap) OR — more likely — the in-memory cache has one slot pointing to the new team while the DB still shows the old team row present. Either way, state is inconsistent across restarts.
**Vulnerability:** There is no `BEGIN IMMEDIATE` / `BEGIN EXCLUSIVE` transaction, no `PRAGMA busy_timeout`, no uniqueness constraint on `team`, and no check that the caller's prior team is cleared. The entire `assign()` flow is two separate `UPDATE` statements under SQLite's default deferred transaction semantics with autocommit disabled, so a second writer can interleave between step 1 and step 2. The fact that `boss_cog.py:2338` dispatches `AssignConferenceView` with `ephemeral=True` does not prevent concurrency: the same commissioner can open two ephemeral views, and two different commissioners have no mutual exclusion.
**Impact:** Silent owner theft; a member's prior team is never unassigned (ghost ownership); power rankings, trade flow, and `/boss` all resolve through `roster.get_team()` so downstream systems show stale state. Requires manual DB fixup via SQL.
**Fix:** (1) Wrap both UPDATEs in a single `with conn:` transaction using `conn.execute("BEGIN IMMEDIATE")`. (2) Add a third statement at the top: `UPDATE tsl_members SET team = NULL WHERE discord_id = ?` to clear the caller's prior team. (3) Consider adding `PRAGMA busy_timeout = 5000`. (4) Long-term: add a `UNIQUE` partial index on `team WHERE team IS NOT NULL AND active = 1` so the DB enforces "at most one active owner per team" at the schema level.

### CRITICAL #2: DB exceptions at `load()` will leak connections and abort bot startup with no recovery
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:140-191`
**Confidence:** 0.90
**Risk:** Every call path to `load()` (startup in `bot.py:451`, refresh after `assign()`, refresh after `unassign()`) has zero exception handling. If `tsl_history.db` is locked (another process writing), corrupt, or the `tsl_members` table doesn't have the `active` column (schema drift), the `sqlite3.connect()` or `conn.execute()` call raises `sqlite3.OperationalError` or `sqlite3.DatabaseError`:
  1. Connection is NEVER closed (no `with conn:`, no `try/finally`) ⇒ file handle leak + Windows file lock.
  2. `_loaded` stays False forever, but `_by_team` / `_by_id` may have been partially left over from a prior successful load — the atomic swap only happens at the end, which is good, but the exception means the cache is stale with no indication.
  3. `bot.py:451-454` catches it and logs "Failed to load", but every downstream call to `get_owner()`, `get_team()`, etc. now silently returns None because the module-level `_by_team` dict is empty on first startup. `boss_cog.py`, `genesis_cog.py`, `oracle_cog.py`, `sentinel_cog.py` all lose ability to resolve Discord ID → team.
  4. After `assign()` or `unassign()` commits DB changes successfully and then `load()` raises, the DB is updated but the cache is stale. The function returns True regardless of the reload failure — caller thinks "assignment succeeded" but `roster.get_team()` still returns the old team.
**Vulnerability:** No `try/finally` or context manager around `sqlite3.connect`. No defensive column existence check (`PRAGMA table_info(tsl_members)`). No retry on `sqlite3.OperationalError: database is locked`. Cache swap is atomic on success but there is no rollback path on failure — the caller is left without a signal that their successful DB write was paired with a failed cache refresh.
**Impact:** A single transient lock (e.g., `build_member_db.py` writing concurrently) silently corrupts in-memory state for the rest of the session. Owner lookups throughout the bot return None, cascading into broken `/boss assign`, broken trade flow team resolution, broken sentinel enforcement. Connection leaks on Windows can also cause subsequent write failures across the process.
**Fix:** Wrap all `sqlite3.connect()` calls in `with sqlite3.connect(db_path) as conn:` or try/finally. Catch `sqlite3.Error` at the function boundary, log with stack trace, and preserve the prior cache (do not clear). Return the actual row count or raise — do not silently return 0. In `assign()` / `unassign()`, if the post-write `load()` raises, re-raise so the caller can surface the failure instead of reporting success with stale cache.

### WARNING #1: `assign()` does not clear the caller's prior team assignment
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:267-277`
**Confidence:** 0.95
**Risk:** If a member already owns the Bengals and the commissioner reassigns them to the Bears, the code only clears the *new* team's prior holder:
```
UPDATE tsl_members SET team = NULL WHERE team = 'CHI' AND discord_id != caller
UPDATE tsl_members SET team = 'CHI' WHERE discord_id = caller
```
Nothing clears the caller's old CIN row. The schema has one `team` column per member, so the old `team='CIN'` value is *overwritten* by the second UPDATE (the `tsl_members` table has one row per member, not one row per assignment). This particular leak only matters if there are ever *two rows* for the same `discord_id` (which would be a data bug) — but the test `WHERE team = ? AND discord_id != ?` is nonetheless incomplete reasoning and will leak on any future schema change to row-per-assignment.
**Vulnerability:** The intent is "one row per member" but the code's correctness depends on that invariant implicitly. There is no `UNIQUE(discord_id)` constraint visible here and `build_member_db.py` is the only enforcer.
**Impact:** On any schema evolution toward row-per-assignment history, this code produces ghost ownership with zero test coverage to catch it.
**Fix:** Add an explicit `UPDATE tsl_members SET team = NULL WHERE discord_id = ?` as the first step inside a transaction, then the clear-rivals step, then the set step. Makes the intent explicit and robust to schema drift.

### WARNING #2: `roster.load()` called at startup before guarding that `tsl_members` table/columns exist
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:148-155`
**Confidence:** 0.80
**Risk:** The query references `active = 1`. `build_member_db.py:1107` shows `active INTEGER DEFAULT 1` — that column was added at some point. Any deploy where `tsl_history.db` is older than that migration will raise `sqlite3.OperationalError: no such column: active`, aborting `load()`. There is no try/except for schema drift and no "add column if missing" guard. Per the ATLAS focus block: "`sportsbook_cards._get_season_start_balance()` must wrap in `try/except sqlite3.OperationalError`. Column may not exist on older DBs." — this file violates the same rule.
**Vulnerability:** Cold-start bootstraps on machines with fresh checkouts / missing migrations will crash silently into the `bot.py:453` exception handler, leaving the bot running without any roster data.
**Impact:** Bot boots, commands work, but every `get_team(user_id)` returns None. Trade flow, `/boss`, sentinel all break. Diagnosis requires grepping `[Roster] Failed to load` in stdout.
**Fix:** Either run the migration on startup or wrap the SELECT in `try: ... except sqlite3.OperationalError: ...` and fall back to `SELECT ... WHERE team IS NOT NULL AND team != ''` without the `active` clause. Log a migration warning when the fallback path runs.

### WARNING #3: Synchronous sqlite in `assign()` / `unassign()` blocks the Discord event loop
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:249-310`
**Confidence:** 0.90
**Risk:** Both functions use blocking `sqlite3.connect()` and are invoked from Discord interaction callbacks. `_TeamAssignSelect.callback` at line 439-459 calls `assign()` directly with no `asyncio.to_thread`. `boss_cog.py:2355` wraps `roster.unassign()` in `run_in_executor` — which proves the author knew it should be offloaded — but the in-file `_TeamAssignSelect.callback` at line 443 calls `assign(self._member.id, team_abbr)` synchronously inside a Discord coroutine. This blocks the gateway event loop for the duration of two UPDATEs + commit + close + full `load()` re-read + reindex. Under SQLite lock contention (e.g., `build_member_db.py` writing), this can block for seconds.
**Vulnerability:** Event loop blocked ⇒ gateway heartbeat misses, other interactions time out, WebSocket may disconnect. Violates the ATLAS rule "Blocking calls inside async functions (sqlite3, requests, time.sleep). All blocking I/O must go through `asyncio.to_thread()`." Partial acknowledgment already exists as W6 in NIGHTLY_REVIEW.md but only mentions the problem, not the inline call site at line 443.
**Impact:** Gateway disconnect under load; delayed response to the commissioner; possible interaction timeout (3s Discord limit) if load() has to retry on lock.
**Fix:** Either (a) change `_TeamAssignSelect.callback` to `await interaction.response.defer()` + `await asyncio.to_thread(assign, self._member.id, team_abbr)` + `followup.send(...)`, OR (b) make `assign()` / `unassign()` / `load()` themselves async-aware with `asyncio.to_thread()` at the entry point. Option (a) is safer because the three are reused from multiple call sites.

### WARNING #4: `_team_cache` is never invalidated when `dm.df_teams` reloads
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:72-102`
**Confidence:** 0.75
**Risk:** `_ensure_team_cache()` checks `if not _team_cache: _build_team_cache()`. Once the cache is populated, it is *never* refreshed. If `data_manager` reloads `df_teams` (e.g., on API refresh mid-season, team rename, divisional realignment), `_team_cache` still holds the stale (abbr, nickname, conference) mapping. `OwnerEntry.team_name` and `OwnerEntry.conference` are also computed once at `load()` time and never updated. Any callsite reading `get_owner("CHI").team_name` will show the stale name until the next `roster.load()`.
**Vulnerability:** No hook wires `data_manager.load_all()` → `roster._team_cache.clear() + roster.load()`. The startup sequence runs in the right order once, but any mid-session API refresh is invisible to roster.
**Impact:** Team renames, division changes, or franchise moves (all in-scope for a sim league) are invisible to roster until bot restart or explicit assign/unassign.
**Fix:** Expose `_team_cache.clear()` or add `roster.refresh_teams()` that clears `_team_cache` and rebuilds `_by_team[X].team_name / conference`. Call from the same codepath that calls `dm.load_all()`.

### WARNING #5: `build_owner_options` can return 0 options and crash the select with HTTPException
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:317-334` and `C:/Users/natew/Desktop/discord_bot/roster.py:360-368`
**Confidence:** 0.80
**Risk:** `discord.ui.Select` requires `min_values=1, max_values=1` with a non-empty `options` list at construction time. `_OwnerListSelect.__init__` passes `options=options` straight through. `OwnerSelectView._show_conference` at 418-425 guards against empty options before constructing the view (good), but the `build_owner_options` function itself has no min/max guard and no 25-option cap. If a conference ever has >25 assigned owners, Discord raises `HTTPException: Invalid Form Body` at `interaction.response.edit_message`. With ~31 teams split as 16/16 AFC/NFC, this is bounded now, but there is no defensive slice like `options[:25]` (compare `build_team_options` at line 357 which *does* slice). Inconsistent defense.
**Vulnerability:** If the league ever expands (the CLAUDE.md says "95+ Super Bowl seasons" and "~31 active teams" — it's already close to the cap), the view silently breaks.
**Impact:** `/boss` and any owner-picker flow crashes on view construction with an HTTPException in the command handler; Discord shows "interaction failed".
**Fix:** Cap the return at `options[:25]` to match `build_team_options`. Alternatively, split large conferences across multiple selects or divisions.

### WARNING #6: `exclude_id` check uses truthy test that skips `exclude_id=0`
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:326`
**Confidence:** 0.60
**Risk:** `if exclude_id and e.discord_id == exclude_id:` — if a caller ever passes `exclude_id=0` (which is not a valid Discord ID but is a plausible sentinel), the filter is skipped. More pragmatically, the pattern `if exclude_id` is a code smell for "I conflate None and 0" and will silently fail if someone passes 0 expecting "exclude no one". The correct idiom is `if exclude_id is not None and e.discord_id == exclude_id`.
**Vulnerability:** Only matters if `exclude_id=0` is ever passed, which current callers don't do. Low-probability bug class.
**Impact:** Filter silently no-ops.
**Fix:** `if exclude_id is not None and e.discord_id == exclude_id: continue`.

### WARNING #7: `assign()` / `unassign()` return True without signaling cache-refresh failure
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:278-283` and `C:/Users/natew/Desktop/discord_bot/roster.py:306-310`
**Confidence:** 0.85
**Risk:** Both functions commit DB changes, close the connection, then call `load(db_path)` to refresh the in-memory cache. If `load()` raises (locked DB, corrupt file, schema drift per W2), the exception propagates past `return True`, and the caller never knows the DB was updated but the cache is stale. Worse, because `load()` is called *after* `conn.close()` and the earlier commit, there is no rollback possible — the DB write has landed. The calling UI at line 443-459 assumes `True` means "success, cache is current" and renders a success embed to the commissioner, who then runs a follow-up command that reads stale cache data.
**Vulnerability:** No distinction between "DB write succeeded and cache is current" and "DB write succeeded but cache refresh threw". The commissioner has no way to know they need to run a manual reload.
**Impact:** After a raised `load()`, subsequent `get_team()` calls return the prior assignment, and the commissioner sees success while the bot shows stale state. Debugging requires reading stdout logs.
**Fix:** Wrap `load(db_path)` in its own try/except. On failure, log `log.exception(...)`, and either (a) return a tuple `(db_ok, cache_ok)` or (b) raise a custom `CacheRefreshError` that the caller can display.

### OBSERVATION #1: No permission check anywhere in `roster.py`
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:249-310, 428-498`
**Confidence:** 0.85
**Risk:** `assign()`, `unassign()`, `_TeamAssignSelect.callback`, and `AssignConferenceView.afc_button/nfc_button` contain no `is_commissioner()` or `is_tsl_owner()` check. The docstring says "Commissioner Only" (line 246) but nothing enforces it — the views are constructed and handed out by `boss_cog.py` which presumably has the decorator at the slash command level, but any code path that constructs `AssignConferenceView(member)` directly bypasses the commissioner gate. This is a defense-in-depth gap, not an active exploit (only `boss_cog` constructs the view today), but the written API is trivially misusable.
**Vulnerability:** Anyone who imports `roster` and calls `roster.assign(their_user_id, 'BUF')` bypasses all permissioning.
**Impact:** Any future cog that accepts user input and forwards to `roster.assign()` has no in-module guard.
**Fix:** Accept an `interaction` parameter (or `author_id`) and check `permissions.is_commissioner(author_id)` inside `assign()` / `unassign()`. Or document loudly that callers MUST gate at the command level.

### OBSERVATION #2: `OwnerSelectView` is dead code per docs/plans
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:394-426`
**Confidence:** 0.80
**Risk:** `docs/superpowers/plans/2026-03-25-my-team-quick-select.md:712` explicitly marks `OwnerSelectView` as "Not currently used by any cog — dead code". The class remains in the file with full logic and child view `_OwnerListView`. Dead code in a trust-critical subsystem is a landmine for future refactors.
**Vulnerability:** Dead code drifts from the rest of the file over time (e.g., if `exclude_id` semantics change, only the live paths are updated).
**Impact:** Cognitive overhead; false positives on "this pattern is used here so it must be correct".
**Fix:** Either delete `OwnerSelectView` + `_OwnerListView` + `_OwnerListSelect`, or add a `# LIVE CALLERS: (none — reserved for future use)` comment so future maintainers know. Per CLAUDE.md, dead files belong in `QUARANTINE/`.

### OBSERVATION #3: `dm is None` degradation returns "" for conference instead of raising or fallback
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:98-102` and `C:/Users/natew/Desktop/discord_bot/roster.py:178-180`
**Confidence:** 0.75
**Risk:** `_team_conference()` returns empty string `""` when `dm` is None or the abbr is unknown. At `load()` line 179, this empty string is stored as `OwnerEntry.conference`. Then `get_by_conference("AFC")` filters by exact equality — any entry loaded while `dm.df_teams` was `None` silently has `conference=""` and is dropped from both AFC and NFC filters. The member is in `_by_id` but invisible to any conference-based query.
**Vulnerability:** If `roster.load()` runs before `dm.load_all()` completes (possible on a fast reconnect), every entry gets `conference=""`. On next refresh, nothing triggers a re-derive of conference — the entries are permanently miscategorized until bot restart.
**Impact:** `build_owner_options("AFC")` returns zero options; `_show_conference` hits the "No assigned owners in AFC" path even though there are 16.
**Fix:** Either raise explicitly when `dm.df_teams is None` at `load()` time (startup contract: "load AFTER data_manager.load_all()"), or re-derive team_name/conference lazily from `_ensure_team_cache()` at read time instead of caching on the `OwnerEntry`.

### OBSERVATION #4: No index on `tsl_members.team` — scan on every assign
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:268-271`
**Confidence:** 0.50
**Risk:** `UPDATE tsl_members SET team = NULL WHERE team = ? AND discord_id != ?` — if there's no index on `team`, this is a full scan of tsl_members. At 88+ rows per CLAUDE.md this is trivially fast, but if the table grows (e.g., row-per-season history), this becomes O(N) per assign.
**Vulnerability:** Not a current bug; scale concern.
**Impact:** None today.
**Fix:** Defer. Add a partial index `CREATE INDEX IF NOT EXISTS idx_tsl_members_team ON tsl_members(team) WHERE team IS NOT NULL` if the table ever grows.

### OBSERVATION #5: `get_all_teams()` re-iterates df_teams on every call — no memoization
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:105-117`
**Confidence:** 0.40
**Risk:** `get_all_teams()` builds the full team list from scratch on every call. `build_team_options` at line 343 calls it, and the select menu can be re-rendered frequently. This iterates 32 rows and is cheap, but it's worth noting that `_team_cache` already has the data — the function could be rewritten to use it.
**Vulnerability:** None today; design smell.
**Impact:** Microseconds of wasted CPU per click.
**Fix:** Reuse `_team_cache` to build the team list. Low priority.

### OBSERVATION #6: No type hint on `callback_fn` / `callback` parameters
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:363, 380, 405`
**Confidence:** 0.50
**Risk:** `callback_fn` / `callback` are untyped. Callers can pass sync or async, zero-arg or multi-arg functions. The code assumes `async def f(interaction, entry)` but there is no type guard. A typo in the caller yields a runtime `TypeError` inside the Discord callback, which Discord swallows into an ephemeral error message.
**Vulnerability:** Contract erosion; no static checking for downstream callers.
**Impact:** Bugs show up only at click time, deep in the async callback path.
**Fix:** Add `Callable[[discord.Interaction, OwnerEntry], Awaitable[None]]` type hints and import `Awaitable, Callable`.

### OBSERVATION #7: `_OwnerListView.back` button may fail silently if parent view has expired
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:386-391`
**Confidence:** 0.65
**Risk:** The back button passes `self._parent` (an `OwnerSelectView` with `timeout=180`). If the parent has timed out (all items disabled), `edit_message(view=self._parent)` still succeeds but the user sees a disabled view with no indication that the interaction expired. There is no handling of `self._parent` being None or `.is_finished()`.
**Vulnerability:** UX smell, not a correctness bug.
**Impact:** User confusion on long-idle picker flows.
**Fix:** Check `self._parent.is_finished()` and send a fresh ephemeral message instead.

### OBSERVATION #8: `load()` leaks `conn` on unexpected exception path
**Location:** `C:/Users/natew/Desktop/discord_bot/roster.py:148-155`
**Confidence:** 0.85 (overlaps with CRITICAL #2 but distinct enough to call out)
**Risk:** `conn = sqlite3.connect(db_path)` is followed by an unprotected `conn.execute(...).fetchall()` and then `conn.close()`. Any exception between open and close leaks the file handle. `assign()` at 255-279 and `unassign()` at 291-307 have the same pattern — if the second UPDATE raises, `conn.commit()` and `conn.close()` never run, leaving the transaction implicit-rolled-back but the handle open until GC.
**Vulnerability:** On Windows, sqlite file handle leaks combine with the exclusive lock model to cause downstream writers to fail until GC.
**Impact:** Cascading DB lock failures on repeated failures.
**Fix:** Use `with sqlite3.connect(db_path) as conn:` everywhere. The context manager commits on success and rolls back on exception, and the handle is always released.

## Cross-cutting Notes

Two patterns observed here likely recur across Ring 1:

1. **Unguarded sqlite3 in async context.** `roster.py`, and per grep likely other `*_cog.py` files, use bare `sqlite3.connect()` inside Discord interaction callbacks. The NIGHTLY_REVIEW already flags this for `roster.assign/unassign` as W6 but the inline `_TeamAssignSelect.callback` path is a third unguarded site in the same file. A systemic audit across all cogs for `sqlite3.connect` inside `async def` would be worthwhile.

2. **No transaction wrapper on multi-statement writes.** The `clear-then-set` two-statement sequence in `assign()` is the classic TOCTOU shape. Other subsystems that do read-modify-write on the same table (build_member_db, economy_cog, flow_wallet) should be audited for the same pattern. `flow_wallet` at least has `reference_key` idempotency — `roster` has neither idempotency keys nor transactions.

3. **Cache staleness after successful DB write.** The `assign()` / `unassign()` pattern of "commit, then refresh cache" is a two-phase commit without rollback — if the cache refresh fails, the DB and cache diverge silently. Other modules that maintain an in-memory cache backed by SQLite should be checked for the same pattern.

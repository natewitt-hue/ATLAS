# Adversarial Review: build_member_db.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 1608
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (3 critical, 7 warnings, 7 observations)

## Summary

This file is the trust root for identity resolution across ATLAS, and while the core `build_member_table()` upsert logic is thoughtfully defended against `UNIQUE` conflicts, several resolver and mutator paths leak connections on exceptions, perform writes without `reference_key`-equivalent idempotency guards, and trust unvalidated input dicts from `discover_guild_members()`. The most dangerous gap is a real `db_username` collision problem: four `db_username` values (`TrombettaThanYou`, `Chokolate_Thunda`, `NEFF`, `Swole_Shell50`) are shared between an active and an inactive row by design, but `resolve_db_username()` caches silently into whichever row it touches — combined with the lowercased alias-map collisions, any downstream lookup that resolves by `db_username` can return the wrong `discord_id`. Ship this only after connection leaks, the `games.homeUser` NoneType guard, and the `discover_guild_members` KeyError guard are fixed.

## Findings

### CRITICAL #1: `resolve_db_username()` leaks connections on unexpected exceptions in read path, and silently commits partial state on race

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1296-1391`
**Confidence:** 0.85
**Risk:** The function wraps the body in `try/finally` with `conn.close()`, but it opens the connection *before* the try (`conn = sqlite3.connect(...)`) on line 1310 — the try is line 1311. If `sqlite3.connect()` itself raises (database locked, disk full, schema drift), no connection is opened so no leak — that path is fine. However, a far worse problem sits inside the function: it calls `conn.execute(UPDATE ...)` followed by `conn.commit()` on lines 1341-1345 *outside any transaction boundary* on a connection that was opened in autocommit mode and has had multiple prior `SELECT`s. If this function is called concurrently from two cogs (e.g. `/ask` and `/atlas` simultaneously — both call identity resolution), both may read `db_username IS NULL`, both may lookup `teams`, both may write different values (API returned different case), and the last writer wins with no conflict detection. Worse, the function uses `print(f"[MemberDB] Auto-resolved ...")` with `sid` that is captured once, so the race is invisible in logs.
**Vulnerability:** No `BEGIN IMMEDIATE` / no `SELECT ... WHERE db_username IS NULL AND discord_id = ?` CAS pattern before UPDATE. No reference_key-style idempotency. SQLite default isolation level with autocommit interleaves reads and writes.
**Impact:** Two concurrent `resolve_db_username()` calls for the same `discord_id` (which is guaranteed to happen given this is called from `oracle_cog`, `codex_cog`, and `/atlas` simultaneously on bot startup or during bursty user traffic) can produce different cached `db_username`s in the `tsl_members` table depending on order, with no error surfaced. Because this is the *trust root* for identity, the entire `/ask` NL→SQL pipeline and all downstream stats can silently attribute actions to the wrong player.
**Fix:** Wrap the read-then-write in `conn.execute("BEGIN IMMEDIATE")` and add a WHERE guard: `UPDATE tsl_members SET db_username = ? WHERE discord_id = ? AND db_username IS NULL` so the write only succeeds if nobody else filled it first. If `cur.rowcount == 0` after UPDATE, re-read and return whatever is now there. Also add `log.exception` on failure instead of letting exceptions bubble out of the `finally` — currently the caller gets a raw sqlite exception.

### CRITICAL #2: `get_db_username_for_discord_id()` and every helper leaks connection on exception

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1281-1293, 1394-1404, 1407-1460, 1463-1474, 1477-1485, 1488-1521`
**Confidence:** 0.95
**Risk:** Every read helper in this file opens `conn = sqlite3.connect(db_path, timeout=10)` at the top, performs one or more `conn.execute(...)`, then calls `conn.close()` — but none of them use `try/finally` or `with conn`. If *any* `.execute()` or `.fetchall()` raises (disk full, database locked past timeout, schema mismatch, `UnicodeDecodeError` on a bad row), the `close()` line never runs and the connection is leaked until GC eventually collects it (which in CPython's refcount GC may be immediate, but on PyPy or under an exception propagating across threads is not guaranteed).
**Vulnerability:** CLAUDE.md lists "Resource leaks: sqlite connections never closed" as a concurrency hazard. These functions are called from `get_alias_map()` on every startup and from `_ask` / resolver paths that run under user traffic. On SQLite under WAL mode, leaked connections hold a shared lock on the WAL file and block checkpointing.
**Impact:** Over long uptime (days), SQLite WAL file grows unbounded because connections never release their shared locks, causing disk pressure and eventual "database is locked" errors across the entire bot — not just this module.
**Fix:** Convert every helper to the `try/finally` pattern or `with contextlib.closing(sqlite3.connect(...)) as conn:`. The existing `resolve_db_username()` already uses `try/finally` — mirror that pattern across `get_db_username_for_discord_id`, `get_known_users`, `get_alias_map`, `get_username_to_nick_map`, `get_active_members`, `upsert_member`, `sync_db_usernames_from_teams`, `validate_db_usernames`.

### CRITICAL #3: `sync_db_usernames_from_teams()` orphan detection crashes on NULL game username

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1226-1239`
**Confidence:** 0.80
**Risk:** The orphan detection query on lines 1227-1229 filters with `homeUser != '' AND homeUser IS NOT NULL`, but the next block iterates `for (gu,) in all_game_users:` and immediately calls `gu.lower()` on line 1236. If the `UNION` side of the query returns a NULL row (which happens if the filter on one half of the union is bypassed because `UNION` deduplication interprets filters per-branch, and the `awayUser` branch here correctly has its own filter — but the branch *combines* distinct results, so any historical row where `homeUser` was inserted with a trailing whitespace value that equals `' '` passes the `!= ''` filter and then `.lower()` works fine, BUT — a NULL slipping through the `awayUser` filter due to historical sqlite behavior on mixed collations would cause `AttributeError: 'NoneType' object has no attribute 'lower'`). The real problem is simpler: the query returns `DISTINCT homeUser ... UNION ... DISTINCT awayUser`, and if an older `games` row has `homeUser` as a non-NULL non-empty string but the value contains only whitespace, `gu.lower()` returns the same whitespace and gets added to `orphans`, polluting the log.
**Vulnerability:** Even under the happy path where the SQL is correct, if `games.homeUser` schema was created with `TEXT` type and any historical import wrote literal `NULL` bytes or mis-encoded bytes, `.lower()` will raise or produce garbage. The whole function is wrapped in nothing — any exception propagates up to the caller in `bot.py:429` (`sync_result = member_db.sync_db_usernames_from_teams()`) which has *no* try/except around it. A crash here aborts `on_ready()` startup entirely.
**Impact:** A single corrupted `games.homeUser` value in the historical DB can crash `on_ready()` and prevent the bot from ever coming online after a reconnect. This is a hot path.
**Fix:** Add `if gu is None: continue` and `gu = gu.strip()` at the top of the loop. Wrap the entire orphan detection block in `try/except Exception as e: print(f"[MemberDB] Orphan detection failed: {e}")`. Also wrap the call site in `bot.py:429` in `try/except` mirroring the `validate_db_usernames` error handler on lines 434-439.

### WARNING #1: `discover_guild_members()` trusts unvalidated dict keys — silent KeyError on malformed input

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1524-1582`
**Confidence:** 0.95
**Risk:** The function signature takes `members: list[dict]` and then uses `m["discord_id"]`, `m["username"]`, `m["display_name"]` with *bracket access* on lines 1556, 1565-1566, 1570, 1578-1579. If a caller passes a dict missing any of these keys (e.g. a guild member with `username=None` for a webhook/bot user, or an early-return Discord.py object that failed to hydrate), this raises `KeyError` inside the `try/finally`, the `finally` closes the connection, and the exception propagates to `bot.py:551` where `run_in_executor` re-raises it on the awaiting task. That task has no `try/except` wrapper in the caller — confirmed by reading `bot.py:548-552`.
**Vulnerability:** Discord.py guild member iteration can yield edge-case members (outage-state members, members whose cache failed to populate). A single bad entry corrupts the entire `discover_guild_members` call.
**Impact:** One misshapen guild member aborts the entire discovery pass. All subsequent members are skipped, the print output lies about which are missing, and the on_ready continuation (`auto-discover guild structure` on line 554) may not even run.
**Fix:** Use `m.get("discord_id")`, `m.get("username", "")`, `m.get("display_name", "")` and `continue` if `discord_id` is falsy. Log a warning when skipping malformed members.

### WARNING #2: `discover_guild_members()` never updates `active` status for kicked/banned members

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1555-1572`
**Confidence:** 0.85
**Risk:** The function iterates the *live guild members* passed in and marks them known/new/updated. But it never reads the delta the other direction: `tsl_members` rows whose `discord_id` is *not* in the live guild anymore. Kicked or departed members retain `active=1` forever. Compare this to `MEMBERS` seed list entries like `Bryan_TSL` (line 564, kicked) and `NewmanO64` (line 534, lurker) that were manually edited to `active=0` — this mutation has to happen by hand because the discovery loop only flows inbound.
**Vulnerability:** CLAUDE.md states `tsl_members` is the single source of truth for identity resolution. If a departed owner's row stays `active=1`, they appear in `get_active_members()` (line 1477), in alias maps, and possibly in leader boards — identity resolution drifts from reality.
**Impact:** Stale `active=1` for departed members silently pollutes downstream stats, rankings, and "current owner" lookups. The next commish `/boss assign` call for a new owner of their old team may conflict.
**Fix:** After the inbound loop, add a second pass: `SELECT discord_id FROM tsl_members WHERE discord_id IS NOT NULL AND active=1 AND discord_id NOT IN (<live ids>)`. For each, set `active=0` and log a departure. Alternatively, track via a dedicated `last_seen_in_guild` timestamp and mark inactive after N days.

### WARNING #3: Four `db_username` collisions in seed data cause alias-map overwrites

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:109-122, 609-624, 171-184, 791-803, 216-229, 1060-1074, 291-305, 895-909, 1407-1460`
**Confidence:** 0.90
**Risk:** AST-parsed `MEMBERS` list confirms 4 `db_username` values are shared between an active and inactive row:
- `TrombettaThanYou` — active `Jordantromberg` (line 112) + inactive `TrombettaThanYou` (line 613)
- `Chokolate_Thunda` — active `chokolate` (line 174) + inactive `ChokolateThunda` (line 793)
- `NEFF` — active `Odyssey63` (line 219) + inactive `NEFF` (line 1063)
- `Swole_Shell50` — active `Sheldon_Scott` (line 294) + inactive `ShellyShell` (line 898)

`get_alias_map()` (lines 1407-1460) queries `WHERE db_username IS NOT NULL` with no `active=1` filter, then populates `alias_map[alias.lower()] = target` in iteration order. The last row processed wins. Since SQL ordering is undefined without an `ORDER BY`, the alias map for `trombettathanyou`, `chokolate_thunda`, `neff`, and `swole_shell50` can return either the active or the inactive entry depending on SQLite's internal row ordering — which changes after VACUUM, index rebuilds, or even certain UPDATEs.
**Vulnerability:** `/ask` calls `fuzzy_resolve_user("neff")` expecting `Odyssey63` (active) — may or may not get it depending on SQLite mood.
**Impact:** Non-deterministic identity resolution on aliases shared between current and historical members. Bug manifests as "sometimes my stats are right, sometimes ATLAS thinks I'm a different player."
**Fix:** Add `ORDER BY active DESC, id ASC` to both SELECT queries in `get_alias_map()` and `get_db_username_for_discord_id()` paths so the active row always wins. Alternatively, exclude `active=0` rows from alias-map building entirely.

### WARNING #4: `get_alias_map()` crashes on NULL `db_u` via `.lower()` if schema drifts

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1422-1437`
**Confidence:** 0.70
**Risk:** The first query filters `WHERE db_username IS NOT NULL`, but line 1433 does `alias_map[db_u.lower()] = target` with no null check. If the filter is ever corrupted by schema drift (e.g. a migration that adds a computed column aliased as `db_username`, or a `JOIN` introduced without re-asserting the filter), `db_u` could be None and `.lower()` raises `AttributeError`. Also, if any row has `db_username` as the literal string `"None"` (from a bad CSV import or manual edit), it passes the IS NOT NULL filter but returns the literal lowercased `"none"` as an alias key — poisoning the map.
**Vulnerability:** Defensive filtering is only as strong as the query, and there is no post-fetch guard.
**Impact:** Alias map can be poisoned or the function can raise at startup, preventing `on_ready()` completion.
**Fix:** Add `if not db_u: continue` at the top of the loop body, and reject literal-string `"none"` / `"null"` as defensive pre-filter.

### WARNING #5: `build_member_table()` COALESCE order on `active` field silently reverses state

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1146-1159`
**Confidence:** 0.85
**Risk:** The upsert `ON CONFLICT` clause sets `active = excluded.active` on line 1157 — *not* `COALESCE(excluded.active, tsl_members.active)`. This is inconsistent with every other field in the clause and is arguably correct in isolation (we want seed data to drive active status), BUT it means: if a commissioner manually sets `active=0` at runtime via `UPDATE tsl_members SET active=0 WHERE discord_username='Jordantromberg'`, the next call to `build_member_table()` (which runs on every bot startup) silently restores `active=1` from the seed data. There is no way to persistently mark someone as departed without editing the seed list.
**Vulnerability:** Runtime/seed state divergence. The seed data assumes it's the single source of truth but runtime mutations happen (e.g. `sync_db_usernames_from_teams` writes `db_username`, some hypothetical future cog may toggle `active`). The doc comment on line 1081 says "runtime team assignments survive bot restarts" — which is true for `team` because of the special COALESCE ordering on line 1154 — but the same protection is absent for `active`.
**Impact:** Departed members keep reappearing as active on every bot restart. Partially overlaps with WARNING #2.
**Fix:** Either document the invariant ("runtime cannot change active status — edit seed") and enforce it via a CHECK, or change line 1157 to `active = COALESCE(excluded.active, tsl_members.active)` so runtime wins.

### WARNING #6: `upsert_member()` does not use `_build_lock` — races against `build_member_table()` concurrent calls

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1488-1521`
**Confidence:** 0.75
**Risk:** `build_member_table()` wraps its entire operation in `with _build_lock:` on line 1088 and uses `BEGIN IMMEDIATE` on line 1116 to serialize writes. `upsert_member()`, which is the single-row equivalent called by `/boss assign` paths (per the function docstring), opens its own connection with no lock and no explicit transaction. On the happy path this is fine because SQLite's per-statement write lock serializes, but if `upsert_member()` is called concurrently *with* a running `build_member_table()` that's inside its `BEGIN IMMEDIATE`, the `upsert_member()` connection will block for up to `timeout=10` seconds and may raise `sqlite3.OperationalError: database is locked`. There is no try/except around the writes.
**Vulnerability:** `build_member_table()` is called on startup *and* after `/boss sync`; `upsert_member()` is potentially called during the same window. No coordination.
**Impact:** Lost writes or visible errors on `/boss assign` during startup. Low-probability but not zero given ATLAS admin usage patterns.
**Fix:** Acquire `_build_lock` in `upsert_member()` as well, or use `conn.execute("BEGIN IMMEDIATE")` + retry-on-lock pattern.

### WARNING #7: `validate_db_usernames()` swallows `games` table absence without distinguishing from "no ghosts"

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1245-1278`
**Confidence:** 0.80
**Risk:** On line 1254-1256, if the `games` table doesn't exist, the function returns `[]` — which is also the return value when all members are valid. The caller in `bot.py:434` logs "ghosts" only when the returned list is non-empty. A fresh install or a corrupted `tsl_history.db` missing the `games` table will silently report "no ghosts" when in fact validation was skipped entirely. Compare with `sync_db_usernames_from_teams()` lines 1189-1191 which returns a `"reason"` key to distinguish — this function has no such signal.
**Vulnerability:** Silent false negative on validation. This directly contradicts bot.py's stated intent (per bot.py CHANGELOG comment on line 48: "validate_db_usernames bare except now logs instead of silently discarding the error") — the bare except was fixed, but the structural silent-skip remains.
**Impact:** Operator loses visibility into validation status. Bad `db_username` entries in the seed data can persist undetected until a user hits them.
**Fix:** Return a dict `{"ghosts": [...], "reason": "games table not found"}` or raise a specific `ValidationSkipped` exception. Update the caller in `bot.py` to log the skip reason.

### OBSERVATION #1: `MEMBERS` list hardcodes 68 entries — fragile source of truth

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:45-1075`
**Confidence:** 0.75
**Risk:** The file contains a 1000-line literal `MEMBERS` list of dicts. Every edit requires code changes, a git commit, and a redeploy. The docstring says the table is the "single source of truth" but the *table* is actually a cache of a *list literal* which is the real source of truth. The documented `discord_id: None` pattern for 40 of 68 entries means the registry cannot resolve these users by Discord ID and depends on `discover_guild_members` to populate IDs over time — but `discover_guild_members()` only inserts to `new_members` list for printing, it never actually inserts them into `tsl_members` (confirmed on lines 1569-1570: it only appends to a local list). Combined with the `CLAUDE.md` rule against ghost usernames, the registry slowly drifts from reality.
**Vulnerability:** Architecture smell, not an immediate bug. But the claim "single source of truth" is stronger than the implementation delivers.
**Impact:** Newly joined Discord members never auto-populate `tsl_members` — they only appear in the "NOT in registry" print warning until a human manually edits the file.
**Fix:** Have `discover_guild_members()` actually `INSERT OR IGNORE` the new members as pending rows (`status='Pending', active=0`) so they become visible in the DB even before manual curation.

### OBSERVATION #2: `resolve_db_username()` uses `difflib.get_close_matches` with `cutoff=0.70` — no cross-match guard

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1362-1378`
**Confidence:** 0.85
**Risk:** 0.70 is a loose cutoff. Discord usernames like `troypeska` and `topshotta338` both match `troy` at around that threshold under some locale settings. The function then *caches* the result permanently into `tsl_members.db_username` — a wrong fuzzy match becomes a permanent misidentification that pollutes every subsequent lookup. There is no human-in-the-loop confirmation, no logging of the similarity score, no protection against cross-matching one user to another user's actual db_username.
**Vulnerability:** Silent data corruption of the trust root. If the fuzzy match picks `Jordantromberg` for some new user `jordan2024` and caches it, the new user's stats forever appear under JT.
**Impact:** Low-probability but high-impact identity mis-attribution. Hard to detect because the cache persists across restarts.
**Fix:** Increase cutoff to 0.90, log the similarity score, and only cache when confidence is very high. For sub-0.90 matches, return the candidate but do NOT write it to the DB — require a commissioner to confirm via `/boss identify`.

### OBSERVATION #3: `get_username_to_nick_map()` excludes active=0 members without documentation

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1463-1474`
**Confidence:** 0.60
**Risk:** The docstring says "Only includes members with both a db_username and a nickname" but the SQL does NOT filter by `active=1`. Historical members like `Villanova46` (nickname `Nova`, line 644) and `PNick12` (nickname `PNick`, line 659) will appear in the map — which is possibly correct for historical stats rendering but ambiguous for current display contexts. Without a filter parameter the caller cannot choose.
**Vulnerability:** Contract ambiguity. The consumer (`stats_hub_cog._USERNAME_TO_NICK`) may surface departed members in "current owners" UIs.
**Impact:** Minor UX confusion. Not a correctness bug.
**Fix:** Add an `active_only: bool = False` parameter or document explicitly that historical members are included.

### OBSERVATION #4: `build_member_table()` uses raw `print()` — no log level, no structured logging

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1219-1221, 1239, 1346, 1386, 1577-1581, 1586-1608`
**Confidence:** 0.50
**Risk:** All diagnostics go through `print()` with emoji decorations. There's no logger, no timestamps, no correlation IDs, no log level filtering. On a production deployment the whole file is silent or noisy based on stdout capture, and structured log consumers (e.g. systemd journal filters, log shipping to monitoring) cannot filter by severity.
**Vulnerability:** Observability gap. CLAUDE.md lists "Observability gaps that would hide failure" as a priority surface.
**Impact:** Harder to detect and root-cause identity resolution failures in production.
**Fix:** Use the `logging` module. Create a module-level logger `log = logging.getLogger(__name__)` and replace `print` with `log.info`, `log.warning`, `log.exception`.

### OBSERVATION #5: `_build_lock` is a `threading.Lock` but functions may be called from `run_in_executor` and from synchronous main — mixed concurrency model

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:22, 1088`
**Confidence:** 0.55
**Risk:** The lock is module-level and `threading.Lock` — fine for thread-based concurrency via `run_in_executor`. But if any async cog ever calls `build_member_table()` directly from the event loop thread (instead of via `run_in_executor`), the lock acquire may block the loop. There's no assertion about which thread owns the call. The default executor thread pool in Python is shared — pool exhaustion could serialize all DB work.
**Vulnerability:** Architecture smell only, no bug yet.
**Impact:** Future maintainers may introduce event-loop blocking without realizing the lock is sync-only.
**Fix:** Document the invariant: "must be called via `asyncio.to_thread` or `run_in_executor`, never from the event loop thread."

### OBSERVATION #6: `if __name__ == "__main__"` block opens a fresh connection without `try/finally`

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1585-1608`
**Confidence:** 0.40
**Risk:** The CLI entry point opens `conn = sqlite3.connect(DB_PATH, timeout=10)` on line 1593 and closes it on line 1597 — unwrapped. If the `execute` or `fetchall` raises (unlikely but possible), the connection leaks. Minor because this is a one-shot script, not a long-running service.
**Vulnerability:** Consistency with the rest of the file's connection handling.
**Impact:** None in practice.
**Fix:** Wrap in `with contextlib.closing(sqlite3.connect(...)) as conn:` for style consistency.

### OBSERVATION #7: No schema migration path — `CREATE TABLE IF NOT EXISTS` hides column additions

**Location:** `C:/Users/natew/Desktop/discord_bot/build_member_db.py:1093-1110`
**Confidence:** 0.80
**Risk:** The table definition is pinned to the current column set. If a future revision adds a column (e.g. `last_seen_at`, `verified_at`), `CREATE TABLE IF NOT EXISTS` on an existing database will silently *not* add the new column, and subsequent `INSERT` statements that reference it will raise `sqlite3.OperationalError: no such column`. There is no migration check — no `PRAGMA table_info` inspection, no ALTER TABLE, no schema version field.
**Vulnerability:** CLAUDE.md priority surface: "Schema migration safety."
**Impact:** Silent breakage on column additions. Would require manual DB surgery on production.
**Fix:** Add a lightweight migration helper: query `PRAGMA table_info(tsl_members)`, compare against expected columns, `ALTER TABLE ADD COLUMN` for any missing. Store a schema version in a `schema_version` table.

## Cross-cutting Notes

**Trust-root contract broken in two places.** The file claims to be the single source of truth for identity resolution, but (a) the `MEMBERS` list literal is the *real* source of truth that the table mirrors, and (b) four `db_username` values are shared between active/inactive rows in the seed data with no deterministic active-preference in the resolver. Downstream modules that read `tsl_members` (per CLAUDE.md: `oracle_cog`, `codex_cog`, `ability_engine`, `roster`, every `_resolve_owner` call site) inherit the non-determinism. Fixing WARNING #3 alone requires edits to every reader that queries by `db_username` — they should all be audited for the same issue.

**Connection leak pattern is ubiquitous in this Ring.** Every helper function in this file follows the same `conn = connect(); execute(); close()` antipattern without try/finally. This is a file-wide smell; other ring-1 files (`codex_utils`, `intelligence`, `reasoning` based on the audit directory listing) may have the same issue and should be spot-checked with the same lens.

**`resolve_db_username()` vs `get_db_username_for_discord_id()` duplication.** Two resolver functions exist with overlapping contracts (1281 vs 1296). `get_db_username_for_discord_id` is described as "the most reliable resolver" but `resolve_db_username` is described as "replaces the old". Which callers still use the old one? Grep for both names across the codebase to verify the migration completed — the docstring claim on line 1308 ("This replaces the old") is aspirational, not enforced.

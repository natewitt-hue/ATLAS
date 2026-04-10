# Adversarial Review: setup_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 974
**Reviewer:** Claude (delegated subagent)
**Total findings:** 19 (3 critical, 8 warnings, 8 observations)

## Summary

This file is the source of truth for channel routing and is loaded second in the cog chain — so its bugs propagate broadly. The most serious problems are: (1) `/setup` is not actually commissioner-gated (only the Discord client UI hides it via `default_permissions`); (2) the `_channel_cache` is invalidated incoherently between guild-scoped writes and unscoped reads, so cross-guild cache poisoning is possible; (3) `_save_channel_id` opens a fresh connection per call inside a per-channel loop with no transaction batching, magnifying the race window with `sync_tsl_db`'s atomic-swap. Fix the auth gap and cache key inconsistency before next deploy.

## Findings

### CRITICAL #1: `/setup` lacks server-side commissioner enforcement

**Location:** `setup_cog.py:938-962`
**Confidence:** 0.95
**Risk:** Any user with the right slash-command permission entry — or anyone in a guild where the integration permission has been altered, copied, or where `default_permissions` is overridden by a guild admin via Server Settings → Integrations → ATLAS — can run `/setup`. `default_permissions(administrator=True)` is a *client-side hint* only; Discord lets guild admins re-grant the command to roles or even @everyone via the integration UI. There is no `await is_commissioner(interaction)` check, no `@commissioner_only()` decorator, and no `interaction.user.guild_permissions.administrator` check inside the function body.
**Vulnerability:** All three sub-flows (`SetupChoiceView.remap_existing`, `create_new`, `nuke_channels`) inherit no auth gating either. The `nuke_channels` button calls `guild.create/delete` and wipes `server_config` rows for the entire guild. A non-admin who reaches the view (e.g. via re-grant, integration override, or by being added to a role with command access) can permanently mass-delete channels.
**Impact:** Total destruction of guild structure by an unprivileged user, plus destruction of every other cog's routing config (cleared by `_clear_guild_config`). This is the worst-case scenario the file ostensibly protects against.
**Fix:** Add `@commissioner_only()` from `permissions.py` to the `setup_command`. Inside `SetupChoiceView.__init__`, store the invoking user ID and add an `interaction_check()` override on the View so only the original commissioner can press the buttons. Also re-verify on each button: `if not await is_commissioner(interaction): return await interaction.response.send_message("Forbidden", ephemeral=True)`.

---

### CRITICAL #2: `_channel_cache` key inconsistency causes cross-guild cache poisoning

**Location:** `setup_cog.py:114-189`
**Confidence:** 0.90
**Risk:** Two write paths and three read paths use mismatched cache keys. `_save_channel_id` (line 189) invalidates `f"{key}:{guild_id}"` where `guild_id` is always a real int. But `get_channel_id` (line 124) reads from `f"{key}:{guild_id or 0}"` — when callers pass `guild_id=None`, the read key is `"foo:0"` and the matching DB row may belong to a *different* guild (the `LIMIT 1` query at line 150 returns whichever row SQLite picks first). The cached entry then survives across guilds and across re-provisioning, because no `guild_id=None` write path ever invalidates `"foo:0"`.
**Vulnerability:** In multi-guild deployments — or in any guild whose first call to `get_channel_id("admin_chat")` happened before guild_id was known (e.g. from a global error handler, a DM, or `bot.py:493`'s global lookup) — the cache will be primed with one guild's channel ID and serve it forever to every other guild. `_save_channel_id` only invalidates `f"{key}:{guild.id}"`, never `f"{key}:0"`, so the poison persists permanently.
**Impact:** Guild A's admin notifications get sent to Guild B's admin-chat. Sportsbook posts land in the wrong guild's casino category. Highly likely to leak admin-only content (force requests, complaints, audit logs) across guild boundaries — a privacy/trust violation in the multi-guild case.
**Fix:** Pick one canonical key format: either always require `guild_id` (force callers to pass it), or always normalize to `guild_id or 0` in BOTH read and write paths AND in the invalidation call at line 189. After `_save_channel_id`, also `_channel_cache.pop(f"{key}:0", None)`. Also fix `bot.py:493` to pass `guild_id` when known.

---

### CRITICAL #3: `nuke_channels` does substring match `"ATLAS "` and silently loses categories

**Location:** `setup_cog.py:546-563`
**Confidence:** 0.85
**Risk:** The match `if "WITTGPT" in name_upper or "ATLAS " in name_upper:` (line 548) deletes any category whose name contains the substring `"ATLAS "` (with trailing space). This will match user-created categories like `"ATLAS LEAGUE"`, `"ATLAS FRANCHISE STATS"`, `"ATLAS ARCHIVE"`, or any community renaming. There's no allowlist against `REQUIRED_CHANNELS`' categories, no confirmation prompt, and no dry-run.
**Vulnerability:** A single button press destroys every matching category and all its channels recursively. The error swallow at line 554 (`except Exception as e: print(...)`) means if Discord rate-limits mid-delete, the operation silently partial-fails and the user sees only `"Cleanup Complete"`. There is also no transaction around `_clear_guild_config` (line 562), so config is wiped even if the Discord deletes failed.
**Impact:** Catastrophic, unrecoverable data loss. Channels are permanently deleted; messages, threads, pins are gone. Combined with CRITICAL #1 (no auth gate), any unprivileged user with command access can wipe the entire guild's ATLAS-prefixed channel tree.
**Fix:** (a) Change the substring match to an exact comparison against the categories listed in `REQUIRED_CHANNELS`. (b) Add a `discord.ui.Modal` confirmation requiring the user to type the guild name. (c) Wrap the destructive sequence in a try/except that aborts and logs on the first failure, rather than continuing. (d) Move the `_clear_guild_config` call to AFTER all Discord deletes succeed.

---

### WARNING #1: Silent `except Exception: pass` in member enrichment hides schema drift

**Location:** `setup_cog.py:799-803`
**Confidence:** 0.95
**Risk:** Catches ALL exceptions during the member status update loop and discards them silently. The comment claims "tsl_members table may not exist yet" but the bare except will also swallow: SQL syntax errors after a schema migration, integrity violations, locked database errors, OOM, KeyboardInterrupt-converted-to-SystemExit, and bugs in the enrichment loop itself.
**Vulnerability:** This runs at every bot startup for every guild. If a column rename or constraint change ever happens to `tsl_members`, the bot will silently stop syncing role-derived statuses and no one will notice until weeks later when an admin asks "why isn't <user> showing as League Owner?" The atlas_focus block explicitly prohibits silent except in admin-affecting paths.
**Impact:** Role-status sync rot. Members with the Commissioner role won't be marked Admin in `tsl_members`. Downstream features (commissioner detection in Codex, admin lookups in Echo) will silently degrade.
**Fix:** Catch only `sqlite3.OperationalError` (table missing) and `sqlite3.IntegrityError`. Use `log.exception("Member enrichment failed for guild %s", gid)` for everything else. Add a `_member_enrichment_seen_error` flag to suppress repeat noise.

---

### WARNING #2: Casino bridge silent except hides cog load failures

**Location:** `setup_cog.py:902-909`
**Confidence:** 0.90
**Risk:** `except Exception: pass` swallows ImportError, AttributeError, and any error from `casino.casino_db.set_setting`. The comment says "Casino module may not be loaded yet" but at startup `auto_discover` is called from `on_ready()` *after* all extensions are loaded (`bot.py:556`), so the casino module IS loaded — any failure here is a real bug, not a missing-module condition.
**Vulnerability:** If `set_setting` raises (locked DB, schema mismatch, type error from `str(ch_id)` if `ch_id` is somehow not coercible), the bridge silently fails and casino games will keep posting to the wrong channel forever, with no log entry. Same code at lines 369-378 has the same pattern.
**Impact:** Casino games (blackjack, slots, crash, coinflip) post to the default channel or fail silently after a re-provisioning. No user-visible error, no log line.
**Fix:** Replace with `except (ImportError, ModuleNotFoundError):` for the actual not-loaded case, and `except Exception: log.exception("Casino bridge failed")` for the rest.

---

### WARNING #3: Auto-discovery skips already-configured channels even when they're stale

**Location:** `setup_cog.py:659-664`
**Confidence:** 0.85
**Risk:** The loop body skips any `config_key` already present in `existing_keys` from the DB. There is no validation that the stored `channel_id` still corresponds to a real, accessible Discord channel. If a guild admin deletes a channel out-of-band, the stale row remains forever and the bot keeps trying to post to a dead ID.
**Vulnerability:** No reconciliation step. The only way to clear a stale row is to invoke the `nuke_channels` button (which has its own problems) or manually edit the DB. New channels with the same name will not be re-mapped because the stale key already exists.
**Impact:** Permanent broken routing after any out-of-band channel deletion. Bot logs will quietly accumulate `discord.NotFound: Unknown Channel` 404s.
**Fix:** During auto-discover, validate each `existing_keys` entry against `guild.get_channel(channel_id)`. If `None`, delete the stale row before iterating REQUIRED_CHANNELS so the matching pass can re-bind to a fresh channel.

---

### WARNING #4: `_provision_channels` writes config row before category creation can fail

**Location:** `setup_cog.py:311-348`
**Confidence:** 0.80
**Risk:** Inside the loop, `_save_channel_id` is called as soon as the channel is found or created. There is no transaction wrapping the loop. If the DB write succeeds for the first 5 channels and then `discord.create_text_channel` raises a `discord.HTTPException` (rate limit, network error) on channel #6, the partial state is committed: rows for channels 1–5 are persisted, channels 6–18 are missing, and re-running `/setup` will skip the persisted ones in the auto-discover path due to the `existing_keys` check (Warning #3).
**Vulnerability:** Partial-failure recovery is brittle. There is no idempotency token or rollback. Each channel's `_save_channel_id` opens its own SQLite connection (line 177) — adding 18 connection-open/close cycles to the critical path during provisioning.
**Impact:** Half-provisioned guilds. Re-running `/setup` may create duplicate channels with `-2` suffixes if the original creation succeeded on Discord but the DB write failed (network blip between line 333 and line 340).
**Fix:** Collect all (key, ch_id) pairs in memory during the loop. After the loop, open ONE connection and `executemany()` with a transaction. On exception, neither the channel creations nor the DB writes are atomic with each other — at minimum log a warning that re-running may produce duplicates.

---

### WARNING #5: `_save_channel_id` ON CONFLICT does not include `guild_id` in conflict key

**Location:** `setup_cog.py:179-187`
**Confidence:** 0.95
**Risk:** The `server_config` table declares `config_key TEXT PRIMARY KEY` (line 104) — meaning `config_key` is unique GLOBALLY, not per-guild. The `_save_channel_id` ON CONFLICT clause exploits this and updates the row regardless of which guild it belongs to. So provisioning Guild B for `admin_chat` will OVERWRITE Guild A's `admin_chat` row.
**Vulnerability:** This is a fundamental schema bug. Every channel key collides across guilds. The table cannot represent multi-guild config at all. Combined with the `LIMIT 1` query in `get_channel_id` (line 150), the surface symptom is that the bot in a multi-guild deployment shares one config across all guilds.
**Impact:** Multi-guild support is silently broken. Guild B's setup wipes Guild A's config. The cache poisoning in CRITICAL #2 then propagates this corrupted state.
**Fix:** Change schema to `PRIMARY KEY (config_key, guild_id)` and migrate existing rows. The migration must dedupe per (config_key, guild_id) pair. Update ON CONFLICT to `ON CONFLICT(config_key, guild_id) DO UPDATE`. Also update `get_channel_id` to never fall through to the unscoped lookup.

---

### WARNING #6: `_provision_channels` migration block uses wrong connection-context

**Location:** `setup_cog.py:351-367`
**Confidence:** 0.75
**Risk:** Uses `with sqlite3.connect(...) as con:` — but `sqlite3.Connection.__exit__` only commits/rolls back the *transaction*, it does NOT close the connection. The connection leaks until garbage collection. This is a known sqlite3 footgun.
**Vulnerability:** Repeated `/setup` invocations leak file descriptors in a process that runs for weeks at a time. WAL mode means stale connections can hold onto WAL checkpoints, growing the WAL file unbounded.
**Impact:** File descriptor exhaustion eventually; WAL bloat sooner. Pattern is repeated nowhere else in this file (the rest use explicit `try/finally con.close()`), so this is an outlier mistake.
**Fix:** Replace with the explicit `try/finally con.close()` pattern used everywhere else in this file.

---

### WARNING #7: `_resolve_owner`-equivalent missing for category name match

**Location:** `setup_cog.py:299-302, 322`
**Confidence:** 0.70
**Risk:** Categories are matched by `cat.name.upper()`. If a guild already has a category named `"ATLAS - Command Center"` (en-dash) or `"ATLAS – Command Center"` (em-dash) instead of the U+2014 em-dash used in `REQUIRED_CHANNELS`, the lookup misses and the bot creates a duplicate category with the right dash.
**Vulnerability:** The category names in `REQUIRED_CHANNELS` use the em-dash character (`—`). Discord's display of em-dashes can vary. Any user-created category with a visually-similar dash will not match, and the bot will create a duplicate, polluting the guild structure.
**Impact:** Silent duplicate categories on re-provisioning, especially after upgrades from older ATLAS versions that used different naming.
**Fix:** Normalize all dashes (`—`, `–`, `-`) to a single character before comparison, and canonicalize whitespace.

---

### WARNING #8: `auto_discover` snapshot of `member.role_ids` is taken outside the executor but iterated inside it

**Location:** `setup_cog.py:865-868, 783-797`
**Confidence:** 0.70
**Risk:** The list comprehension `{r.id for r in m.roles}` runs on the event loop (line 867). `member.roles` traverses Discord's internal role lookup which is fine, but for guilds with thousands of members and dozens of roles each, this is an O(M*R) loop on the heartbeat thread BEFORE the work is offloaded. With ~31 active TSL teams and dependents the count is low, but the pattern doesn't scale and the comment "Collect guild snapshot (CPU-only, no I/O)" understates the cost.
**Vulnerability:** Heartbeat blocking on a large guild. The same applies to the channels/roles/emojis comprehensions (lines 853-887).
**Impact:** Guilds with thousands of members will see heartbeat warnings ("Shard heartbeat blocked for X seconds") at startup. Currently low-priority but worth noting.
**Fix:** If guild membership grows large, move the snapshot construction itself into `run_in_executor` by passing the raw guild object — but discord.py objects are not thread-safe, so the current structure is actually correct and just slow. An alternative is to chunk the snapshot with `await asyncio.sleep(0)` between members.

---

### OBSERVATION #1: `_role_cache` and `_channel_cache` are module-level mutables, never bounded

**Location:** `setup_cog.py:94, 616`
**Confidence:** 0.85
**Risk:** Both caches grow unboundedly across the lifetime of the process. There is no eviction, no TTL, no LRU. In a long-running bot serving multiple guilds, the channel cache grows by up to `len(REQUIRED_CHANNELS) * num_guilds` entries. For a small TSL deployment this is fine; for a multi-tenant deployment it accumulates.
**Vulnerability:** Memory bloat is the least of it — staleness is the real concern. If a channel is renamed or recreated and `_save_channel_id` isn't called, the cache serves the old ID forever. Combined with CRITICAL #2's invalidation gaps, the cache can serve poison values indefinitely.
**Fix:** At minimum, add an `_invalidate_all()` helper called from `on_guild_remove` and `auto_discover`. Long-term, switch to a TTL cache (e.g. `cachetools.TTLCache`) with a 5-minute lifetime.

---

### OBSERVATION #2: `_ensure_table()` re-runs PRAGMA journal_mode=WAL on every call

**Location:** `setup_cog.py:97-111`
**Confidence:** 0.95
**Risk:** Setting WAL mode is persistent on the database file — once enabled, it stays on. Re-issuing the PRAGMA on every connection open is wasted work and may fail if another connection holds an exclusive lock at startup time.
**Fix:** Set WAL mode once at module import time (or at first connection) and remove from `_ensure_table`.

---

### OBSERVATION #3: `_provision_channels` print statements should be log calls

**Location:** `setup_cog.py:281, 318, 326, 332, 345, 348, 361, 367, 376, 378, 380`
**Confidence:** 1.0
**Risk:** This file uses `print()` for nearly all observability. The module also imports `logging` and uses `log.warning(...)` once (line 294) and `log.error(...)` once (line 158), so the inconsistency is intentional but unjustified. Print statements bypass log levels, log filters, log handlers, and structured log aggregation. They're invisible if stdout is captured by systemd or piped to /dev/null.
**Fix:** Convert all `print(...)` calls to `log.info(...)` or `log.warning(...)` as appropriate.

---

### OBSERVATION #4: `get_channel_id_async` cache check duplicates `get_channel_id` cache check

**Location:** `setup_cog.py:165-172`
**Confidence:** 0.95
**Risk:** Both check the cache before delegating. The `run_in_executor` call to `get_channel_id` will check the cache *again*. Not a bug, just redundant work. More importantly: if the cache is hit on the async wrapper, the executor is never used — but if the cache is hit *inside* `get_channel_id` after spawning the executor, you've spent thread-pool time for no reason.
**Fix:** Either (a) inline the DB read in the executor and skip the recursion into `get_channel_id`, or (b) accept the redundancy as harmless.

---

### OBSERVATION #5: `_clear_guild_config` cache invalidation iterates the entire dict

**Location:** `setup_cog.py:202-204`
**Confidence:** 0.90
**Risk:** `[k for k in _channel_cache if k.endswith(f":{guild_id}")]` builds a Python list, scans every cache key, and does string comparison. For a large cache this is O(N). It's also fragile: a key like `"admin_chat:12"` would incorrectly match a guild_id of `2` because of `endswith`. Use `:` as a known suffix delimiter and parse it explicitly.
**Fix:** Either index the cache by guild (`{guild_id: {key: id}}`) or use an explicit suffix check: `k.split(":")[-1] == str(guild_id)`.

---

### OBSERVATION #6: Migration sentinel is global, not per-guild

**Location:** `setup_cog.py:351-367`
**Confidence:** 0.85
**Risk:** The `_migration_v2` row is keyed by `config_key='_migration_v2'` with `guild_id=0`. After it runs once for ANY guild, it never runs for any other guild. Since the table's PRIMARY KEY is `config_key` only (Warning #5), there can only ever be one such row globally. If a new guild joins after migration ran, its orphaned `real_sportsbook` row (if any) will not be cleaned up.
**Fix:** When the schema is fixed (Warning #5), make `_migration_v2` per-guild. Or use a separate `migration_state` table with `migration_name TEXT PRIMARY KEY` instead of overloading `server_config`.

---

### OBSERVATION #7: `auto_discover` opens 5 separate sqlite connections instead of one

**Location:** `setup_cog.py:644-822`
**Confidence:** 0.95
**Risk:** Connections are opened and closed at lines 644, 683, 720, 748, 782, and 810. Each connection initialization on Windows is non-trivial (file lock acquisition, WAL coordination). All of this work runs inside the executor so it doesn't block the event loop, but it's still wasted thread-pool time and increases the contention window with `sync_tsl_db`.
**Fix:** Open one connection at the top of `_auto_discover_db` and pass it to helper sections; close it once at the end.

---

### OBSERVATION #8: `on_guild_join` listener does nothing useful

**Location:** `setup_cog.py:966-969`
**Confidence:** 1.0
**Risk:** The listener just prints two lines. There's no auto-provisioning, no notification to commissioners, no trigger for `auto_discover`. New guilds get zero setup until someone manually runs `/setup` — and there's no way for the new guild's admin to know they need to. The docstring at lines 4-9 claims the cog "Fires on_guild_join to: 1. Create the server_config table... 2. Scan existing channels... 5. Post a setup receipt embed." None of that is true anymore.
**Fix:** Either (a) actually implement the documented behavior (run `auto_discover` on join, post a welcome embed), or (b) update the module docstring to reflect that join now requires manual `/setup`.

---

## Cross-cutting Notes

- **Cache key inconsistency (CRITICAL #2)** likely affects every other cog that calls `get_channel_id` without passing `guild_id`. Audit `bot.py:493`, `permissions.py:183`, and any other call sites for the bug.
- **Schema PK bug (WARNING #5)** is foundational — fix this BEFORE addressing the cache key issue. The cache fix only matters if the underlying table can actually represent per-guild config.
- **Silent except pattern (WARNING #1, #2)** appears twice in this file alone. Worth a sweep across other Ring 1 cogs for the same anti-pattern.
- **Connection-per-call pattern** in `_save_channel_id` will be replicated in any cog that follows this file as a template. The fix (single transaction per provisioning batch) is also applicable.
- **`require_channel` decorator** (in `permissions.py:172`) imports `get_channel_id` lazily inside the predicate — meaning the cache poisoning bug at CRITICAL #2 will leak into every channel-restricted command.

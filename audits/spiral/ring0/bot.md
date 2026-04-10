# Adversarial Review: bot.py

**Verdict:** needs-attention
**Ring:** 0
**Reviewed:** 2026-04-09
**LOC:** 681
**Reviewer:** Claude (substituted for Codex due to upstream rate limit)
**Total findings:** 18 (2 critical, 8 warnings, 8 observations)

## Summary

bot.py is mostly orchestration glue, so most failure surfaces are around cog loading, startup lifecycle, and exception handling — not raw business logic. Two real ship-blockers exist: (1) the cog loader silently degrades when `echo_cog` or `setup_cog` fail to load even though CLAUDE.md says they MUST load first, and (2) `_startup_load()` can hang indefinitely with no timeout while `_startup_done` is already set, leaving the bot stuck in `_data_ready=False` state across all reconnects until full restart.

## Findings

### CRITICAL #1: Cog loader silently degrades when MUST-load-first cogs fail
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:241-266`
**Confidence:** 0.95
**Risk:** If `echo_cog` or `setup_cog` raises during `bot.load_extension(...)`, the loop catches the exception, prints a one-line error, and proceeds to load every other cog. The bot enters a "running but architecturally broken" state where downstream cogs use fallback persona stubs (`get_persona() → "You are ATLAS, the TSL league bot."`) and `setup_cog.get_channel_id()` ImportError fallbacks. Slash commands appear to work but produce wrong-channel routing and stub-quality AI responses.
**Vulnerability:** The except block on line 264 swallows ALL extension load failures uniformly, with no concept of "critical cog" vs "optional cog". CLAUDE.md explicitly states `echo_cog` MUST be first and `setup_cog` MUST be second — but the loader treats them identically to `awards_cog` and `god_cog`. There is no halting check after `_EXTENSIONS[0]` and `_EXTENSIONS[1]`, no `bot.close()`, no exit, no admin-channel notification.
**Impact:** Production incident where the bot is "online" in Discord and accepting commands but every persona response is a fallback stub and every channel routing decision uses ImportError defaults. Hard to detect because the bot looks healthy externally. Diagnosis requires reading startup logs, which most operators don't.
**Fix:** After loading `echo_cog` and `setup_cog` (lines 242-243), check the cog actually registered: `if "EchoCog" not in {type(c).__name__ for c in bot.cogs.values()}: raise RuntimeError("echo_cog failed to load — refusing to start in degraded state")`. Or, more simply, re-raise the exception inside the except block when `ext in ("echo_cog", "setup_cog")`. Optionally surface the failure to `ADMIN_CHANNEL_ID` before bailing.

### CRITICAL #2: `_startup_load()` can hang forever, leaving `_data_ready=False` permanently
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:510-534`
**Confidence:** 0.92
**Risk:** `_startup_done = True` is set on line 517 BEFORE `await loop.run_in_executor(None, _startup_load)` on line 531. If `_startup_load()` hangs (MaddenStats API down, member_db build stuck on a SQLite lock, etc.), `_data_ready` never gets set to True. The bot reconnects → `on_ready` fires → sees `_startup_done == True` → skips reload → `_data_ready` is still False forever. Every interaction returns "ATLAS is still loading league data" and the bot is unrecoverable without a full restart.
**Vulnerability:** No timeout on `dm.load_all()`, `db_builder.sync_tsl_db(...)`, `member_db.build_member_table()`, or any other call inside `_startup_load`. The executor-thread call has no `asyncio.wait_for(...)` wrapper. The "set flag before async work" pattern (intended to prevent concurrent on_ready races) becomes a footgun: it commits to "startup is happening" before verifying it actually completes.
**Impact:** A flaky external API or DB lock during a Discord reconnect window can permanently brick the bot's command surface until manual restart. Sentinel/Oracle/Genesis commands all return the loading message; users have no way to recover.
**Fix:** Either (a) move `_startup_done = True` to AFTER `_data_ready = True` so a hang leaves `_startup_done` False and the next `on_ready` retries, OR (b) wrap the executor call in `asyncio.wait_for(loop.run_in_executor(None, _startup_load), timeout=300)` and on timeout, reset `_startup_done = False` and log loudly. Option (b) is cleaner because it preserves the original race-prevention intent.

### WARNING #1: `_invalidate_caches()` only clears `codex_cog` — silently leaves stale data in every other cog cache
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:215-221`
**Confidence:** 0.85
**Risk:** After a `sync_tsl_db` rebuild, only `codex_cog.clear_query_cache()` is called. Any other cog with its own in-process cache (oracle_cog stat-leader cache, flow_sportsbook odds cache, genesis_cog roster cache, etc.) keeps serving pre-sync data until that cog naturally evicts. Users see fresh data from `/codex` queries but stale data from `/stats` and `/genesis` queries until the next bot restart.
**Vulnerability:** The `except Exception: pass` swallow on line 220-221 means even if `clear_query_cache` raises, no diagnostic surfaces. The function name promises to invalidate "caches" (plural) but only touches one.
**Impact:** Subtle data inconsistency between subsystems after every `/wittsync` and `/rebuilddb`. The kind of bug operators only notice when a user reports "the trade page shows last week's roster".
**Fix:** Maintain a registry of cache-invalidation hooks. Each cog with a cache registers its `clear_cache()` callback in `bot.py` at startup; `_invalidate_caches()` iterates the registry and calls each, logging individual failures. Replace `except Exception: pass` with `log.exception("cache invalidation failed for %s", name)`.

### WARNING #2: `discord.Intents.all()` grants every privileged intent
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:181`
**Confidence:** 0.90
**Risk:** `Intents.all()` enables MESSAGE_CONTENT, PRESENCE, MEMBERS, GUILD_BANS, etc. Two of these (PRESENCE and MEMBERS) require explicit privileged-intent toggles in the Discord Developer Portal. More importantly, this grants the bot read access to every member's online presence and full message content across every channel — far more than needed. Principle of least privilege is violated.
**Vulnerability:** The bot only needs MESSAGE_CONTENT (for `@mention` parsing in `on_message`), GUILD_MESSAGES, GUILDS, and possibly MEMBERS (for `member_db.discover_guild_members`). PRESENCE is unused.
**Impact:** Larger attack surface if the bot token is ever leaked. A compromised token could exfiltrate every member's presence history and full message logs across the entire server. Also: if Discord disables a privileged intent for ToS reasons, the bot fails to start.
**Fix:** Replace with explicit `intents = discord.Intents.default(); intents.message_content = True; intents.members = True; intents.guilds = True`. Add a comment justifying each enabled flag.

### WARNING #3: Every guild member's PII is printed to stdout on every initial boot
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:540-552`
**Confidence:** 0.88
**Risk:** The `for m in sorted(human_members, ...)` loop prints `m.display_name`, `m.name` (Discord username), and `m.id` (snowflake) for every human member. In production, this output likely lands in a log file or terminal scrollback that may persist longer and have wider access than the bot process itself.
**Vulnerability:** Print statements have no audience control. The data is operational PII (Discord username + ID + display name) and there's no log-level guard like `if DEBUG: print(...)`. CLAUDE.md does not mark this as intended behavior.
**Impact:** Privacy violation for league members; potential GDPR/COPPA exposure if the bot serves any minors; log files become a tempting target if leaked.
**Fix:** Demote member listing to `log.debug(...)` behind a `BOT_DEBUG_MEMBERS=1` env flag. Keep the count summary (`{len(human_members)} human members`) at info level. Never log Discord IDs unless explicitly requested for operational debug.

### WARNING #4: `bot.tree.sync()` has no timeout and runs unconditionally on first boot
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:332`
**Confidence:** 0.78
**Risk:** If Discord is rate-limited or returns a 5xx, `await bot.tree.sync()` blocks setup_hook indefinitely (no `asyncio.wait_for`). Worse, every code change deploy runs sync — burning Discord's 200 syncs/day rate limit one slot per restart. CLAUDE.md FIX #9 comment claims this is gated to "initial boot" but `setup_hook` runs on every fresh process start, not just first-ever.
**Vulnerability:** No timeout, no detection of "command tree unchanged", no skip logic when commands are identical to last sync.
**Impact:** Dev iteration loop can exhaust Discord's daily sync quota on a busy debug day, leaving slash commands unrebgistered until midnight UTC.
**Fix:** Wrap in `asyncio.wait_for(bot.tree.sync(), timeout=30)`. Hash the command tree definition and skip sync if the hash matches the last persisted sync (store hash in `flow_economy.db` or a small `.sync_state` file).

### WARNING #5: Bot presence shows "online" before `_data_ready` is True
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:522-534`
**Confidence:** 0.82
**Risk:** `bot.change_presence(...)` is called on line 523, which sets the visible status to "Watching TSL · INTELLIGENCE · OVERSIGHT · AUTHORITY". Then `_startup_load()` runs in the executor (lines 530-531). Then `_data_ready = True` on line 534. During the executor window (potentially several seconds for a full DB rebuild), users see the bot as fully online and try to invoke commands — every one of which returns "still loading league data".
**Vulnerability:** Presence and `_data_ready` are out of order. The "online" presence is a lie until `_startup_load` finishes.
**Impact:** Users hammer commands during cold-start window, get error messages, retry, get more error messages, file complaints. Operators see noisy logs.
**Fix:** Set initial presence to a "Loading league data..." activity, then update to the production presence string AFTER `_data_ready = True` (after line 534). One extra `change_presence` call.

### WARNING #6: `OracleMemory()` instantiated at module import time
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:138-139`
**Confidence:** 0.75
**Risk:** `_atlas_mem = OracleMemory()` runs during `import bot`, before `setup_hook` and before `_startup_load`. If `OracleMemory.__init__` opens a SQLite connection, loads embeddings, or does any I/O, that I/O happens at import time. Errors during import are far harder to debug than errors during normal startup, and they happen on every test run that imports `bot`.
**Vulnerability:** Heavy initialization in module scope. The pattern violates the "import should be cheap" principle.
**Impact:** Unit tests that import bot.py for symbols pay the OracleMemory startup cost. Bot startup failures during OracleMemory init produce cryptic ImportErrors instead of clean runtime errors.
**Fix:** Make OracleMemory lazy: `_atlas_mem: OracleMemory | None = None`, then construct on first use inside `_startup_load()` or `on_message`. Or use a `@functools.cache`d factory function.

### WARNING #7: `setup_hook` exception swallows for non-cog setup are silent in admin channel
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:268-327`
**Confidence:** 0.83
**Risk:** Six separate `try/except Exception as e: print(...)` blocks: flow wallet setup (291-292), parlay backfill (300-301), affinity setup (308-309), HTML render engine init (316-317), sportsbook_core migration (326-327), Playwright drain pool (188-192). Each prints to stdout but does NOT escalate to `ADMIN_CHANNEL_ID`. Operators never know critical infrastructure failed unless they read logs.
**Vulnerability:** Print-only error handling. The `ADMIN_CHANNEL_ID` mechanism exists (line 177-179) but is never used by any of these failure paths.
**Impact:** Casino jackpot tagging silently broken, parlay legs not backfilled, affinity scores not tracked, render engine missing, sportsbook settlement bus not subscribed — and no alert.
**Fix:** Define a helper `async def _notify_admin(msg: str)` that posts to `ADMIN_CHANNEL_ID` if set. Replace each print-only error with `print(...) + await _notify_admin(f"⚠️ {phase} failed: {e}")`. Best-effort send, swallow secondary failures.

### WARNING #8: `bot.start_time` and `_bot_start_time` are two separate time.time() calls
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:519-520`
**Confidence:** 0.65
**Risk:** Two consecutive `time.time()` calls produce two values that differ by microseconds. Any code reading `bot.start_time` gets a slightly different boot timestamp than code reading `_bot_start_time`. Cosmetic in most cases, but a real bug if anything uses the two as a uniqueness key.
**Vulnerability:** Confusion about which is canonical. Probably a copy-paste artifact during a refactor.
**Impact:** Latent. Most likely never observed, but the inconsistency suggests there's no agreed source of truth for "when did this bot start".
**Fix:** `_bot_start_time = time.time(); bot.start_time = _bot_start_time` — single source of truth.

### OBSERVATION #1: `_startup_done` race-prevention comment is misleading
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:514-517`
**Confidence:** 0.90
**Risk:** The comment "Set BEFORE async work to prevent concurrent on_ready races" implies asyncio has a race. It does not — asyncio is single-threaded and `if _startup_done: ... ; _startup_done = True` has no `await` between check and set, so two on_ready callbacks cannot interleave there.
**Vulnerability:** The comment leads future maintainers to think the ordering matters for thread-safety reasons. If they ever refactor and add an `await` between the check and the set, they'll trust the comment and not realize they just opened a real race.
**Impact:** Maintainability / correctness misunderstanding. No current bug.
**Fix:** Reword to "Set before the long executor call so we don't accidentally re-run startup if Discord fires on_ready twice during the slow startup window. (Single-threaded asyncio means there's no in-line race; the protection is against re-entry across the executor await.)"

### OBSERVATION #2: All output uses `print()` instead of `logging`
**Location:** Throughout bot.py (lines 179, 237, 239, 265, 274, 282, 286, 289, 292, 299, 301, 307, 309, 315, 317, 322, 327, 344, 347, 362, 377, 405, 410, 417, 419, 422, 428, 431, 437, 439, 445, 447, 452, 454, 461, 465, 469, 471, 473, 480, 508, 515, 536, 537, 542, 544, 552, 560, 640, 664, 671-675)
**Confidence:** 0.95
**Risk:** No log levels (no way to silence DEBUG without silencing ERRORS), no log rotation, no structured fields, no per-cog logger names, no UTC timestamps. All output is one undifferentiated stream.
**Vulnerability:** Every log statement is `print(f"...")`. Searching/filtering production logs requires grep over plain text. Scaling to multiple bot instances or remote log ingestion means parsing print statements instead of structured records.
**Impact:** Operational hygiene. Hard to triage incidents from logs alone.
**Fix:** Introduce `logging.getLogger("atlas.bot")`. Use `log.info`, `log.warning`, `log.exception`. Configure root logger in `__main__` block with rotation. This is a multi-PR refactor and should not block a release.

### OBSERVATION #3: `ATLAS_ICON_URL` imported but never used inside bot.py
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:171`
**Confidence:** 0.85
**Risk:** `from constants import ATLAS_ICON_URL, ATLAS_GOLD` — neither symbol appears anywhere else in this file. They're imported for side-effect re-export (other cogs can `from bot import ATLAS_ICON_URL`), but that's an anti-pattern.
**Vulnerability:** Dead-looking import that's actually doing magic re-export. Future cleanup of "unused imports" would break dependent cogs.
**Impact:** Confusing for maintenance. Linters would mark these as unused.
**Fix:** Either remove the import (if no cog depends on it), or move the import to a doc-string `"""Re-exported for cogs that import from bot directly: ATLAS_ICON_URL, ATLAS_GOLD"""` so the intent is documented.

### OBSERVATION #4: `from setup_cog import auto_discover` is inside `on_ready`
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:556`
**Confidence:** 0.70
**Risk:** Late-binding import inside an event handler. Probably to avoid a circular import during module load.
**Vulnerability:** None functionally. Slightly slower first call. Future refactor might miss this dependency.
**Impact:** None observed.
**Fix:** Document the reason inline: `# Late import: setup_cog imports from bot for some helpers, so we can't top-level this`. Or restructure to remove the cycle.

### OBSERVATION #5: `validate_db_usernames()` failure logs but does not escalate
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:433-439`
**Confidence:** 0.70
**Risk:** Print-only when validate_db_usernames raises; no admin notification. Same pattern as Warning #7 but lower-priority because validation is a "best effort" check.
**Vulnerability:** Operators never see the validation failure unless they read logs.
**Impact:** Stale db_username mappings persist undetected.
**Fix:** Bundle into the same `_notify_admin` helper from Warning #7's fix.

### OBSERVATION #6: `_data_ready_check` shows the same generic "still loading" message on every retry
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:204-212`
**Confidence:** 0.75
**Risk:** A user trying to run `/stats` while the bot is loading gets an ephemeral message. They retry 30 seconds later and get the SAME message with no indication of progress. They retry 5 minutes later and get the same. Frustrating UX.
**Vulnerability:** Static error message; no estimated-time field; no "try again in N seconds" hint.
**Impact:** Cosmetic. Users complain.
**Fix:** Track approximate progress (e.g., what phase of `_startup_load` is running) in a module-level variable, surface it in the loading message: "ATLAS is loading league data (phase: rebuilding tsl_history.db). Try again in ~30 seconds."

### OBSERVATION #7: Optional-module imports catch ImportError, masking real bugs
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:142-153`
**Confidence:** 0.78
**Risk:** `try: import lore_rag\nexcept ImportError: lore_rag = None` works for "module file missing" but ALSO swallows ImportError raised inside the module (bad relative import, missing dep, syntax error in transitively imported file). The module falls back to None silently and downstream `if lore_rag:` checks wrongly skip features.
**Vulnerability:** ImportError is too broad. A typo in `lore_rag.py` produces silent feature absence, not a clean failure.
**Impact:** Lore RAG context might silently disappear after a refactor; affinity stats might silently stop tracking.
**Fix:** Catch only ModuleNotFoundError (Python 3.6+ has this as a subclass of ImportError specifically for "module not found"). Real bugs inside the module will then bubble up at import time as intended.

### OBSERVATION #8: `blowout_monitor` interval is hardcoded to 15 minutes
**Location:** `C:/Users/natew/Desktop/discord_bot/bot.py:475-508`
**Confidence:** 0.55
**Risk:** No env var override for the interval. Operators can't tune monitoring frequency without code changes. The threshold logic lives in `dm.flag_stat_padding`, but the interval lives in this decorator.
**Vulnerability:** Configuration baked into a decorator literal.
**Impact:** Minor flexibility cost.
**Fix:** Read interval from env var: `BLOWOUT_INTERVAL_MIN = int(os.getenv("BLOWOUT_INTERVAL_MIN", "15"))` and use `@tasks.loop(minutes=BLOWOUT_INTERVAL_MIN)`. Document in README.

## Cross-cutting Notes

The dominant pattern in bot.py is **best-effort startup with print-only error logging**. Every subsystem init (echo cogs, setup cog, flow wallet, parlay backfill, affinity, HTML pool, sportsbook migration) follows: `try: setup; print("...ready") except Exception as e: print(f"...failed: {e}")`. This is appropriate for OPTIONAL subsystems but actively dangerous for REQUIRED ones (echo_cog, setup_cog) because the bot continues running in a degraded state with no operator alert.

The single `_notify_admin` helper proposed in Warning #7's fix would solve six distinct findings (Warning #7, Critical #1's notification half, Observation #5, plus three other print-only error sites). It's the highest-leverage refactor in the file.

The `_startup_done` flag pattern is correct for its stated purpose (preventing duplicate startup work on Discord reconnect), but the FLAG-BEFORE-WORK ordering combined with the absent timeout is what creates Critical #2. Either remove the timeout requirement (move flag set to after work) or add the timeout — currently the code has the worst of both worlds.

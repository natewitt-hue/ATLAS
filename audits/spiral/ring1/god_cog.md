# Adversarial Review: god_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 121
**Reviewer:** Claude (delegated subagent)
**Total findings:** 11 (2 critical, 5 warnings, 4 observations)

## Summary

`god_cog.py` is a thin wrapper around two highest-privilege operations (affinity reset, full DB rebuild) with adequate role gating but glaring gaps for a "god-tier" surface: no audit trail on irrecoverable destructive ops, no second-factor confirmation, a singleton DB rebuild that can be run concurrently by multiple GODs, and a permission model that silently degrades to "anyone with administrator can see the command" via `default_permissions`. The setup hook also swallows load failures and prints instead of logging. Ship-blockers are the missing audit trail/concurrency guard on `rebuilddb` and the missing audit trail on `affinity reset`.

## Findings

### CRITICAL #1: Affinity reset is irrecoverable with no audit trail or confirmation step

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:49-69`
**Confidence:** 0.9
**Risk:** A god-tier user (or anyone with the GOD role compromised) can permanently zero a user's affinity score in a single command invocation. There is no audit log of who reset whom, no timestamp record, no "are you sure" double-confirm, and no record of the prior score. `affinity_mod.reset_affinity()` (per `affinity.py:135-143`) issues a direct `UPDATE` to `flow_economy.db` and clears the in-memory cache — once executed, the prior score is gone forever. Affinity drives ATLAS persona modulation (FRIEND / DISLIKE / HOSTILE) and arguably constitutes user-visible reputation state.
**Vulnerability:** The reset is a bare `bool` parameter on the same command. There is no two-step UI flow (e.g., a confirm button on a `discord.ui.View`), no `log.warning()` or `log.info()` before the call, no insert into any admin audit table, and the success message itself is `ephemeral=True` (line 68), so other commissioners cannot see the action even if they're watching the channel. The ATLAS-specific concern in the prompt explicitly calls out: "Affinity reset: irrecoverable destructive op. Confirmation? Audit trail?"
**Impact:** A malicious or careless GOD user can silently nuke any user's affinity history with zero traceability. In a sock-puppet abuse scenario or a compromised GOD account, this is a one-way information loss with no recovery path. Combined with the ephemeral confirmation, even other administrators cannot see that the action occurred.
**Fix:** Before calling `reset_affinity()`: (1) read the prior score and `interaction_count`; (2) emit `log.warning("[GOD] %s reset affinity for %s (was %.1f, count=%d)", interaction.user.id, user.id, prior_score, prior_count)`; (3) optionally write a row to a `god_audit_log` table in `flow_economy.db` (timestamp, actor_id, action, target_id, prior_state); (4) require a confirmation step via a `discord.ui.View` button — single-click destructive operations on irrecoverable state should never be one-shot. Echo the prior score in the success embed so the actor can manually restore if it was accidental.

### CRITICAL #2: `god rebuilddb` has no concurrency guard — two simultaneous invocations corrupt the swap

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:81-113`
**Confidence:** 0.85
**Risk:** Two GOD users (or one user double-clicking the slash command in the brief window before Discord ack) can fire `god_rebuilddb` in parallel. Both kick off `build_tsl_db.sync_tsl_db()` concurrently. Per `build_tsl_db.py:347-454`, the function writes to `DB_PATH + ".tmp"`, deletes any pre-existing tmp file (line 348-349), and atomically `os.replace`s into `DB_PATH`. Two simultaneous runs will: (a) race on `os.remove(tmp_path)` followed by `sqlite3.connect(tmp_path)` — one process can wipe the other's in-flight tmp file mid-build; (b) race on the final `os.replace`/`os.rename` and `os.remove`/`shutil.copy2` fallback — one of them will see `PermissionError` on Windows, retry 3 times, then `shutil.copy2` over a possibly-half-written file.
**Vulnerability:** The cog does not maintain any `_rebuild_in_progress` lock, asyncio.Lock, or sentinel file. The `await loop.run_in_executor(None, ...)` (line 94-97) will happily schedule two concurrent rebuilds against the default thread pool. There is also no global mutex inside `build_tsl_db.sync_tsl_db()` — it assumes single-caller. A god-tier command above commissioner is the most likely place to be invoked impulsively during an outage when "things look broken," which is exactly when concurrent invocations happen.
**Impact:** Worst case: corrupted `tsl_history.db` swap mid-rebuild, requiring manual restoration from backup. Even the "happy path" race produces a misleading success message for the loser of the race (their result dict reports games/players that were actually written by the other invocation). The downstream `_PRESERVE_TABLES` copy (build_tsl_db.py:395-420) is especially vulnerable — copying old tables into a tmp DB that another process is about to wipe is unrecoverable.
**Fix:** Add a class-level `asyncio.Lock` (`self._rebuild_lock = asyncio.Lock()` in `__init__`) and wrap the entire body of `god_rebuilddb` in `async with self._rebuild_lock:`. If the lock is held, immediately respond with "ATLAS: rebuild already in progress, started by <user>". Better: also add a process-wide sentinel file (`tsl_history.db.rebuild.lock`) checked at the top of `sync_tsl_db()` itself, so cron-driven rebuilds and the slash command can't collide.

### WARNING #1: `default_permissions(administrator=True)` exposes the command — `is_god` only filters at execution

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:43-47, 57-60, 83-86`
**Confidence:** 0.85
**Risk:** The `app_commands.Group` declares `default_permissions=discord.Permissions(administrator=True)`, which is the wrong gate for a "god-tier" command. Discord's `default_permissions` controls which users *see* the command in the slash menu — anyone with the guild administrator bit can see and attempt to invoke `/god`. The actual GOD role check happens only inside the command body via `is_god(interaction)` (lines 57, 83). Per `permissions.py:99-115`, `is_god` only checks the literal `GOD` role on the member; it does not fall back to administrator. So an admin who is *not* a GOD can fire the command, hit the `is_god` check, and see the "requires the GOD role" rejection — which is fine for security, but the command surface is confusing and broadcasts the existence of god-tier operations to anyone with admin.
**Vulnerability:** Discord `default_permissions` and `is_god()` use disjoint criteria. The visibility filter (`administrator`) is much broader than the execution filter (literal GOD role). This is a UX/permission-model smell at minimum, and a discoverability leak at worst.
**Impact:** Any guild admin can see `/god affinity` and `/god rebuilddb` in their slash command picker, learn that ATLAS has god-tier commands, and probe by invoking them. The check then rejects them, but the existence of these commands and their parameters (including the `reset` toggle) is now public knowledge to admins. There is no actual permission bypass.
**Fix:** Either (a) drop `default_permissions=administrator` and rely on `is_god()` alone (commands will be visible to everyone but reject on execution — same as the current model, just more honest), or (b) better, gate at the Group level with a `@app_commands.checks.has_role(GOD_ROLE_NAME)` decorator so Discord's permission UI hides the command from non-GODs. Use `require_god()` from `permissions.py:140-157` consistently — that decorator already exists but is not used here.

### WARNING #2: `rebuilddb` calls `loop = asyncio.get_running_loop()` then runs blocking `dm.get_players()` on the event loop

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:88-97`
**Confidence:** 0.7
**Risk:** Lines 92-93 call `dm.get_players()` and `dm.get_player_abilities()` *before* dispatching to `run_in_executor`. Only the `db_builder.sync_tsl_db()` call itself is moved off-thread (line 94-97). If `dm.get_players()` does any blocking I/O (HTTP fetch, sqlite read, large DataFrame copy), it blocks the event loop while the user waits.
**Vulnerability:** The cog doesn't know whether `dm.get_players()` is cheap (pure DataFrame access from `data_manager`'s preloaded state) or expensive (re-fetch from MaddenStats). If it's the latter, this is a multi-second event-loop block. Even in the cheap case, returning the entire players DataFrame (potentially 1500+ rows of dicts) on the event loop and then handing it across the executor boundary is wasteful.
**Impact:** Discord interaction event loop is stalled during god-rebuild, blocking all other gateway events for the duration. On hot startup or heavy load, this can cause Discord to disconnect the bot.
**Fix:** Move the `dm.get_players()` and `dm.get_player_abilities()` calls *inside* the lambda passed to `run_in_executor`, or call them via `await asyncio.to_thread(dm.get_players)`. Best: have `sync_tsl_db()` itself call `dm` functions internally, and pass nothing across the boundary.

### WARNING #3: `setup()` swallows the cog load exception with a print, hiding load failures from logs

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:116-121`
**Confidence:** 0.85
**Risk:** The `setup()` function catches *every* exception in `add_cog()` and prints a message instead of letting the error propagate. This violates the audit-rule "no silent excepts in admin-facing code" and means a god-tier cog can fail to load without any log entry, traceback, or alert. The print goes to stdout, which on a deployed bot may not be captured anywhere.
**Vulnerability:** A typo, import error, or Discord API change that breaks `add_cog()` produces a single line of stdout and zero log entries. There is no `log.exception(...)`, no re-raise, no admin notification. The `bot.py` cog loader will see the function complete normally and assume god_cog loaded, which silently disables all god-tier admin operations.
**Impact:** ATLAS could ship with `/god` non-functional and no one would notice until someone tried to invoke `/god rebuilddb` during an emergency and got "unknown command." Combined with the lack of admin notification, this is exactly the kind of silent degradation the focus block warns against.
**Fix:** Replace `print(...)` with `log.exception("ATLAS: GOD cog failed to load")` and re-raise. The bot's cog loader should fail loudly when a god-tier cog cannot be loaded — silent failure is much worse than a noisy crash.

### WARNING #4: `db_result["success"]` access can KeyError if `sync_tsl_db` raises before populating

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:98-113`
**Confidence:** 0.6
**Risk:** Line 102 reads `db_result["success"]` immediately after the `try/except`. If `sync_tsl_db()` returns the result dict with `success=False` and `errors=[...]` but no top-level exception, control flows past the try block to line 102. But if `sync_tsl_db()` returns an empty dict, partial dict, or `None` (e.g., if a future refactor changes the return shape), the `["success"]` access raises `KeyError` and the user sees no response — Discord's interaction times out with "this interaction failed" and the cog logs nothing.
**Vulnerability:** No defensive `db_result.get("success", False)` and no second `try/except` around the response-building code. The contract with `sync_tsl_db()` is fragile — any future change to the return shape breaks the cog with no error visibility. Per `build_tsl_db.py:476-484`, the function does set `result["errors"]` and falls through with `success=False` on a fatal exception, so the *current* path is safe — but the gap is one refactor away from a real bug.
**Impact:** Discord interaction failures, no log output, GOD user sees "interaction failed" with no recovery path. Forces the GOD user to restart the rebuild.
**Fix:** Use `db_result.get("success", False)` and wrap the response-building lines in their own `try/except` that calls `interaction.followup.send(...)` with an error message and `log.exception(...)` on failure.

### WARNING #5: `god_affinity` reset path discards prior score with no echo to actor

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:65-69`
**Confidence:** 0.8
**Risk:** When `reset=True`, the cog never reads the prior score before calling `reset_affinity()`. The success message says "Reset affinity for X to 0" but doesn't echo what it was — so if the actor reset the wrong user, they have no way to know what value to manually restore.
**Vulnerability:** `affinity.reset_affinity()` (per `affinity.py:135-143`) does an unconditional UPDATE and clears the cache. There is no "before/after" log, no return value, and the cog doesn't read the prior state. This is the same root cause as CRITICAL #1 but distinct in remediation: even without an audit trail, simply showing the prior score in the response would meaningfully reduce the harm of a mis-click.
**Impact:** Mis-clicks on the wrong user are unrecoverable. The actor cannot even tell whether the reset was a no-op (prior score was already 0) or a substantive change.
**Fix:** Read prior score via `await affinity_mod.get_affinity(user.id)` before the reset, echo it in the success message: `f"Reset affinity for **{user.display_name}** from {prior:.1f} to 0."` Bonus: also surface the affinity tier label that was reset.

### OBSERVATION #1: Module docstring claims "Role hierarchy: GOD → Commissioner → TSL Owner → User" but `is_god` does not include commissioner fallback

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:1-12`, `C:/Users/natew/Desktop/discord_bot/permissions.py:99-115`
**Confidence:** 0.8
**Risk:** The docstring says GOD is above Commissioner. The implementation in `permissions.py:99-115` does NOT make commissioners auto-pass `is_god()` — only the literal GOD role and `ADMIN_USER_IDS` (in DM context) qualify. Compare to `is_tsl_owner()` (`permissions.py:96`) which explicitly falls back to `is_commissioner()`. The asymmetry is undocumented.
**Vulnerability:** A commissioner reading the docstring and naive understanding of "GOD → Commissioner → TSL Owner → User" hierarchy would expect commissioners to have GOD powers in an emergency. They don't. This is fine if intentional (GOD really is a separate, exclusive tier) but the docstring is misleading.
**Impact:** Documentation/code mismatch. A commissioner needing to invoke `/god rebuilddb` during an outage when no GOD is available is locked out, contrary to the documented hierarchy.
**Fix:** Either (a) update the docstring to clarify "GOD is a parallel tier, not above Commissioner — commissioners cannot invoke /god commands", or (b) update `is_god()` in `permissions.py` to fall back to commissioner status as documented. Pick one and align the docstring.

### OBSERVATION #2: `lambda: db_builder.sync_tsl_db(...)` captures `players` and `abilities` by reference

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:94-97`
**Confidence:** 0.5
**Risk:** The lambda passed to `run_in_executor` closes over `players`, `abilities`, and `db_builder` by reference. If `players` or `abilities` are mutable DataFrames or lists shared by `data_manager`, and another cog mutates them while the rebuild is running on the executor thread, you get a TOCTOU bug — `sync_tsl_db()` sees a half-mutated snapshot. Per `_atlas_focus.md`: "DataFrame mutation across cogs (data_manager DataFrames are shared state)."
**Vulnerability:** No defensive copy. The cog assumes `dm.get_players()` returns an immutable snapshot, but the focus block explicitly flags shared DataFrame mutation as a known hazard. This is a mild concern in practice but real if `data_manager` re-loads while the rebuild is running.
**Impact:** Subtle data corruption in `tsl_history.db` if `data_manager.load_all()` runs in parallel with a rebuild. Hard to detect, hard to reproduce.
**Fix:** Defensive copy before crossing the executor boundary: `players_snapshot = list(players)` (or `players.copy()` if it's a DataFrame). Or, better, have `sync_tsl_db()` re-fetch internally and not accept passed-in data at all.

### OBSERVATION #3: Hard-coded emoji and English strings — no i18n hook, no Echo persona

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:59, 63, 68, 84, 99, 103-111, 119, 121`
**Confidence:** 0.4
**Risk:** All user-facing strings are hard-coded English with emoji prefixes. The cog does not call `get_persona()` from `echo_loader.py`. Per CLAUDE.md, ATLAS has a unified persona that "always 3rd person as ATLAS" — the success message "Reset affinity for ..." is in second/imperative form, not ATLAS-voice.
**Vulnerability:** Inconsistent voice with the rest of ATLAS. Not a correctness bug, but breaks immersion — and the prompt explicitly notes the persona rules are "hard-won lessons."
**Impact:** Cosmetic only — but the prompt-block emphasis on Echo persona suggests this should be flagged.
**Fix:** Consider routing user-facing god-cog responses through a small persona helper that wraps them in ATLAS-voice. At minimum, drop the imperative "Reset affinity for X" and use third-person ATLAS voice as documented in CLAUDE.md.

### OBSERVATION #4: `is_god` check is duplicated inline rather than using `require_god()` decorator

**Location:** `C:/Users/natew/Desktop/discord_bot/god_cog.py:57-60, 83-86`, `C:/Users/natew/Desktop/discord_bot/permissions.py:140-157`
**Confidence:** 0.85
**Risk:** Both commands manually call `if not await is_god(interaction): return await interaction.response.send_message(...)` with identical text. `permissions.py` already exposes a `require_god()` decorator (lines 140-157) that does exactly this. The inline pattern is duplicated, drift-prone, and easy to forget on the next god-tier command added.
**Vulnerability:** Two copies of identical permission-rejection text. If the rejection message changes (e.g., to include a contact URL), one gets updated and the other doesn't. New god commands added by future devs may copy-paste the inline pattern and accidentally typo the role name.
**Impact:** Maintenance smell, drift hazard.
**Fix:** Replace both inline checks with `@require_god()` above each command, removing lines 57-60 and 83-86 entirely.

## Cross-cutting Notes

- **Audit trail gap is structural across the admin tier.** No `god_audit_log` table exists in `flow_economy.db` (per `affinity.py:53-67` schema). Every other admin-facing destructive op (boss_cog, sentinel force-grants, balance ops) likely has the same gap. Recommend adding a centralized `admin_audit_log(timestamp, actor_id, action, target_id, before_state, after_state)` table and a small helper module that all admin cogs can call. This finding alone would be a worthwhile follow-up sprint.
- **Concurrency on `sync_tsl_db()` is a single-source-of-truth concern that affects every caller.** Both `god_cog.god_rebuilddb` and `bot.py`'s `wittsync` (presumably) call into `build_tsl_db.sync_tsl_db()` without any process-wide mutex. The fix should live in `build_tsl_db.py` itself (sentinel file or `multiprocessing.Lock`) so all callers benefit.
- **The `default_permissions=administrator` mismatch with `is_god()` is likely repeated in `boss_cog.py` and other admin cogs.** Worth a quick audit of every `app_commands.Group(default_permissions=...)` declaration to confirm the visibility filter matches the execution filter.
- **`setup()` print-instead-of-log pattern is a project-wide smell.** Every cog reviewed in this batch likely has the same `try/except print(...)` block. A grep-pass for `"FAILED to load"` in cog setup hooks would surface them all. Replace with `log.exception()` and re-raise so the bot loader fails loud.

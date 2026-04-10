# Adversarial Review: economy_cog.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 1160
**Reviewer:** Claude (delegated subagent)
**Total findings:** 19 (3 critical, 9 warnings, 7 observations)

## Summary

This file mostly does the right things — admin balance ops are wrapped in `flow_wallet.get_user_lock`, single-tx, and pass `reference_key` (mostly). The stipend loop has clearly absorbed prior bug-fix work. But three real ship-blockers remain: `admin_set` has no `reference_key` plumbing at all (idempotency hole on cron + commissioner button retries), the pre-fix-then-pay ordering in `_process_stipend` opens a window that drops payments on bot crash before payouts complete, and the role-based give/take loops now silently abandon members on the very first per-user error with no recovery path. There are also several silent excepts in admin-facing UI code, a wrong channel-key spelling that means the daily flow-health alert never reaches admins, and zero permission checks on the public `EconomyCog._eco_*_impl` methods (they rely entirely on `boss_cog` for gating — defense-in-depth gone).

## Findings

### CRITICAL #1: `admin_set` has no `reference_key` parameter at all — set_balance() is non-idempotent

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:142-166`
**Confidence:** 0.95
**Risk:** ATLAS rule (CLAUDE.md, "Flow Economy Gotchas") states: *ALL debit/credit calls MUST pass `reference_key`*. The `admin_set` helper neither accepts nor forwards a `reference_key`, and the call to `flow_wallet.set_balance(...)` at lines 151-156 omits it entirely. `flow_wallet.credit/debit` reject duplicate `reference_key`s by reading `_check_idempotent`, but `set_balance` was never designed with idempotency. A Discord interaction retry on `_eco_set_impl` (or a boss-cog button double-click) calls `admin_set` twice with the same args; the second call writes a *second* `economy_log` row and a *second* transaction record. Worse: if the commissioner clicks "Set to 5,000" twice in 200ms, both succeed and both are audited as legitimate edits — there's no way to distinguish double-click from intentional re-set in the audit trail.
**Vulnerability:** No idempotency guard, no per-call `reference_key`, no de-dup on the underlying `set_balance` codepath. The `_eco_set_impl` modal at boss_cog:1226-1227 catches no Discord retry exceptions, so the worst case (modal submission failure → user re-submits → both go through) is wide open.
**Impact:** Silent ledger corruption on commissioner double-action. Audit log shows two "set" events with identical timestamps and the commissioner has no way to tell them apart from a forensic standpoint. Less severe than double-debit (no money is destroyed) but the ledger integrity story is broken.
**Fix:** Add `reference_key: str | None = None` to `admin_set`, default to `f"ADMIN_SET_{discord_id}_{int(...)}"`, and pass it through. Then add `reference_key` support to `flow_wallet.set_balance` (parallel to credit/debit). At minimum, wrap the SQL `INSERT INTO economy_log` block in a `WHERE NOT EXISTS (SELECT 1 FROM economy_log WHERE ...)` guard keyed on `(discord_id, action, amount, admin_id, reason)` within a 5-second window.

### CRITICAL #2: Stipend "mark paid before pay" ordering causes silent payment loss on crash

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:340-413`
**Confidence:** 0.92
**Risk:** Line 367 calls `await mark_stipend_paid(stipend["stipend_id"])` *before* the per-member payout loop (lines 378-406) starts. The comment ("BUG-7 FIX: mark paid BEFORE payouts — prevents double-pay if bot crashes mid-loop") explicitly trades double-pay for *no-pay*. In a 30-member-role stipend, if the bot crashes after paying 3/30 members, `last_paid` is now set and `get_due_stipends()` will not re-yield this stipend until the *next* interval (24h to 720h later). 27 members silently miss this period's payout. The `try/except` at line 408-413 only logs — it does not roll back `mark_stipend_paid`.
**Vulnerability:** The "fix" was to swap "double-pay risk" for "missed-pay risk" without taking advantage of the per-member deterministic `stip_ref` keys (line 385). Since `stip_ref` is `f"STIPEND_{stipend_id}_{member.id}_{last_paid}"`, the underlying `flow_wallet.credit/debit` call is *already idempotent* — if you re-process a partially-paid stipend, the already-paid members short-circuit at `_check_idempotent` and return without changing balance. The original BUG-7 motivation no longer applies because the per-member ref_keys make double-pay impossible. Reverting the order would be safe and correct.
**Impact:** Stipends silently drop payments to 90%+ of role members on any crash mid-loop. No alert, no retry, no audit log entry showing the gap. With 30-member roles paid weekly, a single mid-loop crash costs that role one entire week's stipend with no recovery path short of a manual `_eco_stipend_paynow_impl` call (which checks `last_paid` and bails because it was already marked).
**Fix:** Move `await mark_stipend_paid(...)` to *after* the per-member loop completes successfully. The deterministic ref_key on lines 385-397 already prevents double-pay because `_check_idempotent` short-circuits identical keys. If the loop fails partway, `last_paid` stays unchanged, the stipend re-fires next hour, and already-paid members are no-ops via the existing idempotency layer. As an additional safety, on partial failure log a structured message and post to admin channel so operators see the gap.

### CRITICAL #3: `_eco_give_role_impl` / `_eco_take_role_impl` abandon the entire batch on the first per-user error

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:541-550, 578-588`
**Confidence:** 0.93
**Risk:** Both batch role-payout helpers wrap a `for m in members` loop in a single `try/except`. Any exception on member N — including transient `aiosqlite.OperationalError: database is locked`, an `InsufficientFundsError` from `admin_take` against a user with $0, or a wallet lock contention — breaks out of the loop *and the followup message tells the commissioner the partial count*. Members N+1 through end are silently skipped, no retry, no audit row. There is no per-member `reference_key` for the batch (admin_give/admin_take generate timestamp-based keys *inside* the helper, so re-running the failed command would generate new keys and double-pay the already-paid members).
**Vulnerability:** Combination of (a) no per-member `reference_key` propagated from the batch context, (b) blanket exception handler that aborts the loop, (c) no recovery / continue-on-error semantics, (d) ledger post is *inside* admin_give but the batch itself doesn't post anywhere meaningful. Critically: after a partial failure, the commissioner's natural reaction is to re-run the role payout, which will *double-pay* the first N members because no shared idempotency key spans the batch.
**Impact:** Real money corruption. Imagine a 50-member role payout that fails at member 23 due to db lock. Commissioner sees "paid 22/50, error: database is locked", waits 5 seconds, runs the same `/boss eco give-role` again. Members 1-22 get *paid twice* (timestamps differ → different ref_keys), members 23-50 get paid once. Total over-pay: 22× the per-member amount.
**Fix:** (1) Generate a single batch ref_key prefix at the top of the helper: `batch_id = f"ROLE_GIVE_{role.id}_{interaction.id}"`. (2) Pass `reference_key=f"{batch_id}_{m.id}"` through `admin_give` (which already accepts the kwarg). (3) Switch the loop to `try/continue` with per-member error tracking, not blanket abort. (4) On partial failure, automatically schedule a retry by re-iterating *only* the failed members with the same ref_keys.

### WARNING #1: `flow_health_loop` posts to wrong channel key — admin alerts never delivered

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:325-332`
**Confidence:** 0.97
**Risk:** Line 327 calls `get_channel_id("admin-chat", guild.id)` with the spelled-with-dash key. The canonical key in `setup_cog.py:43` is `"admin_chat"` (underscore). `get_channel_id` returns `None` for unknown keys, the surrounding `if ch_id` short-circuits, and the embed is silently dropped. By contrast, `_post_audit` at line 279 correctly uses `"admin_chat"` — so `_post_audit` works but `flow_health_loop` does not. This is the daily flow-economy health alert; if it has critical/high-severity findings, no one ever sees them.
**Vulnerability:** Inconsistent key naming between the two methods in the *same file*. The bug is silent — the surrounding `try/except` at line 331 catches and logs to `log.exception` but the failure mode is "no critical/high path was taken because ch was None", not "exception raised", so even the log line is misleading.
**Impact:** Daily auditing is dead. The `FlowAuditor` runs and reports correctly to logs, but operators never get a Discord alert for critical findings (e.g., balance corruption, double-credits).
**Fix:** Change `"admin-chat"` → `"admin_chat"` on line 327. Audit every `get_channel_id(...)` call across all cogs for the same typo.

### WARNING #2: `_post_audit` swallows all exceptions silently

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:275-291`
**Confidence:** 0.85
**Risk:** ATLAS rule (CLAUDE.md, "Flow Economy Gotchas") prohibits silent `except Exception: pass` in admin-facing code. Line 290-291: `except Exception as e: print(f"[Economy] Audit post failed: {e}")`. While not literally `pass`, it (a) uses `print` not `log`, so the message bypasses the logging configuration (no level, no rotation, no admin filter), and (b) catches *every* exception including `KeyboardInterrupt`, `MemoryError`, and Discord-specific failures that should fail loud. If `_post_audit` fails to post a "10,000,000 TSL Bucks given" alert, the commissioner's audit trail has a hole and they'll never know.
**Vulnerability:** The print → stdout path is invisible in production. Logger calls go to file/Discord/whatever; print does not. There's no admin-facing alert when audit posts fail, so a commissioner could be making large-money grants while the audit channel sees nothing.
**Impact:** Missing audit entries that look identical to "no admin action happened" — perfect cover for accidental misuse.
**Fix:** Replace `print(...)` with `log.exception("Audit post failed for message: %s", message)`, narrow `except Exception` to `except (discord.HTTPException, discord.Forbidden, discord.NotFound)`, and let any other exception bubble.

### WARNING #3: `_ctx_*` callbacks in FlowHubView swallow `Exception` silently

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:1009-1097` (multiple methods)
**Confidence:** 0.85
**Risk:** Every `_ctx_*` callback wraps the dispatch in `try: ... except discord.NotFound: return`. `_ctx_scratch` (lines 1039-1049) goes further: it catches *bare `Exception`* on line 1045 and only logs by attempting another `interaction.response.send_message` — which itself will fail if the interaction was already responded to, and *that* failure is also silently swallowed (`except discord.NotFound: return` on line 1048). Stack trace lost. This is admin-adjacent code (the Flow Hub is the user-facing economy UI) and any error here is invisible to debugging.
**Vulnerability:** No `log.exception(...)` anywhere in any of the eight `_ctx_*` callbacks (1009, 1019, 1029, 1039, 1051, 1061, 1073, 1083). If `cog.casino_hub.callback(...)` raises `KeyError` because the casino was loaded with a different signature, the user sees nothing and the bug is invisible to operators.
**Impact:** Every Flow Hub button silently no-ops on any error. Users hit a button, nothing happens, no error message. Operators have no breadcrumb.
**Fix:** Add `except Exception: log.exception("FlowHubView._ctx_X failed")` to each. If you want to keep `discord.NotFound` as the silent case (interaction expired), keep it as a separate branch above the catch-all.

### WARNING #4: `_process_stipend` distribution math is wrong for negative stipends

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:368-397`
**Confidence:** 0.78
**Risk:** Lines 372-376 use `abs(stipend["amount"])` for `base_amount` and `remainder` calculation. For a *deduction* stipend (`amount < 0`) on a 7-member role with `amount = -100`, `base_amount = 14`, `remainder = 2`. Then lines 386-397 branch on `if stipend["amount"] > 0` to call give vs take, but each call gets `payout = base_amount + (1 if idx < remainder else 0)` — a *positive* number. So `admin_take(member.id, payout, ...)` gets called with `payout = 15` for the first 2 members and `14` for the rest. The total deducted from the role is `2*15 + 5*14 = 30+70 = 100`, which matches `abs(amount)`. OK, *that* part is correct. But: `admin_take` floors at 0 — if a member only has $5, only $5 is taken even though the role-payout claims $14 was taken. The aggregate deduction across the role will be silently less than the configured amount, with no audit indication.
**Vulnerability:** No total-deducted reporting back from `admin_take`, no warning when actual deduction < requested. The post-audit message at line 416-420 says "Stipend processed — -100,000 TSL Bucks" even if only -73,000 was actually taken because one member was bankrupt. Auditor sees the wrong number.
**Impact:** Stipend deduction reporting is unreliable — the audit log says X was deducted but the books show <X. Reconciliation fails.
**Fix:** Have `admin_take` return both the requested amount and the actual amount; aggregate the actuals; use the actual sum in `_post_audit`. Or, more aggressively, do a pre-flight pass to validate every member has sufficient funds, then commit atomically.

### WARNING #5: `_eco_*_impl` methods have zero permission checks (defense-in-depth gone)

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:428-604` (all `_eco_*_impl` methods)
**Confidence:** 0.78
**Risk:** None of `_eco_give_impl`, `_eco_take_impl`, `_eco_set_impl`, `_eco_check_impl`, `_eco_give_role_impl`, `_eco_take_role_impl`, `_eco_stipend_*_impl` check `is_commissioner(interaction)`. They rely entirely on `boss_cog` to gate access via the commissioner check at the `/boss eco` slash command level. This is correct for the *current* dispatch path, but:
  1. If anyone adds a new entry point (a button view, a context menu, a slash command alias) and forgets the commissioner check, the impl methods happily process the request.
  2. The methods are reachable as bot attributes — `bot.get_cog("EconomyCog")._eco_give_impl(...)` is callable from any cog.
  3. The codebase already documents this as a hazard ("permission decorators missing on commissioner-only commands" — `_atlas_focus.md`).

`_ctx_eco_health` at line 1083-1097 *does* check `is_commissioner` — proving this pattern is supposed to be done in EconomyCog. The other 7 `_eco_*_impl` methods are inconsistent.
**Vulnerability:** Single-layer trust boundary. boss_cog's check is the *only* gate. If boss_cog gets refactored, hot-patched, or has a slash-command name collision (CLAUDE.md "Discord API Constraints" — two cogs with same command name → second silently fails), the gate disappears and the methods become world-callable.
**Impact:** Latent privilege-escalation risk. Real exploit requires a code-path bug in another cog, but the scenario is plausible.
**Fix:** Add `if not await is_commissioner(interaction): return await interaction.response.send_message("Commissioner-only.", ephemeral=True)` to the top of every `_eco_*_impl`. Belt and suspenders.

### WARNING #6: `/flow` slash command has no channel restriction or permission check

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:702-719`
**Confidence:** 0.72
**Risk:** `/flow` is a public slash command but it (a) has no `@require_channel(...)` decorator, (b) does an ephemeral defer immediately (good for users), but (c) has no rate-limiting or per-user cooldown, and (d) starts an `auto_refresh_loop` task that runs for 300 seconds. A user spamming `/flow` every second creates 300 concurrent auto-refresh tasks per user, each polling the Playwright pool every 30s. The Playwright pool is documented as 4 pre-warmed pages — sustained spam will exhaust the pool and degrade everything else (casino renders, sportsbook cards).
**Vulnerability:** Each call to `view.start_auto_refresh()` schedules a new task with no cancellation of older ones. A user who runs `/flow` 5 times in 60s has 5 concurrent loops eating Playwright pages.
**Impact:** Easy DoS-by-accident on the rendering pipeline. The 30-second sleep at line 893 mitigates somewhat but stacking 10+ tasks still hammers the pool.
**Fix:** Add an `app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)` decorator to limit to once every 5s per user. Track existing FlowHubViews per user_id and cancel/replace old ones when a new `/flow` is invoked.

### WARNING #7: `_setup_economy_tables` doesn't run inside `cog_load` for a fresh DB upgrade path

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:40-73, 263-267`
**Confidence:** 0.65
**Risk:** `_setup_economy_tables` only runs once per `cog_load`, and uses `CREATE TABLE IF NOT EXISTS`. If the schema evolves (e.g., adding a `currency` or `subsystem` column to `economy_log`), there's no migration path — the new columns will simply not exist on existing prod databases. The cog will start, the tables-exist check passes, and the next INSERT fails at runtime with `OperationalError: table economy_log has no column named ...`. No version table, no `ALTER TABLE`, no migration directory.
**Vulnerability:** Schema evolution requires manual DB surgery. There's no `db_migration_snapshots.py`-style migration hook for `economy_*` tables. (It exists for other parts of the system per CLAUDE.md — but is not called for economy tables here.)
**Impact:** Future schema changes will silently break the audit trail until someone manually `ALTER TABLE`s on prod.
**Fix:** Add a versioned migration table or use `PRAGMA user_version` to track schema versions. On `cog_load`, compare current version against expected and run migrations.

### WARNING #8: `_process_stipend` uses `str(last_paid) = "init"` for first-time stipends as the ref_key seed — collision risk

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:365, 385`
**Confidence:** 0.7
**Risk:** Line 365: `last_paid = stipend.get("last_paid", "init")`. Line 385: `stip_ref = f"STIPEND_{stipend['stipend_id']}_{member.id}_{last_paid}"`. The very first time a stipend runs, `last_paid` is `None` (not "init", because get with default returns the actual `None` value from the dict, not the default — the key exists in the row dict from `get_due_stipends()`). The ref_key becomes `f"STIPEND_42_12345_None"`. Now, *second* run after `mark_stipend_paid` sets `last_paid` to a real ISO timestamp — different ref_key, fine. But: if `mark_stipend_paid` runs in between two reads of the same stipend dict (race window between `get_due_stipends` and `_process_stipend`), or if a stipend is manually deleted and re-added with the same `stipend_id` (rowid recycling on autoincrement table can happen if the table is dropped/recreated), the `f"STIPEND_42_12345_None"` ref_key from the new row collides with the old one and the credit is rejected as "already processed".
**Vulnerability:** The "init" default is dead code — `stipend.get("last_paid", "init")` evaluates to `None` on the first run because `last_paid` is in the row tuple as `None`, not absent from the dict. The `or "init"` would be required for the intended behavior but isn't there. So the literal string `"None"` ends up in the ref_key, which is fragile.
**Impact:** First-run stipends have an ambiguous key. Edge case but real on stipend re-creation or DB resets.
**Fix:** Use `last_paid = stipend["last_paid"] or "first"` (with the `or`), or better, use `f"first_run_{stipend['stipend_id']}"` for the first-run sentinel and the timestamp for subsequent runs.

### WARNING #9: `admin_take` audit log records `amount` (requested) instead of `actual_take` — misleading

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:108-139`
**Confidence:** 0.83
**Risk:** Lines 119-127 compute `actual_take = min(amount, old_balance)` (correctly floored at 0), but line 134 inserts the *requested* `amount` into `economy_log` instead of `actual_take`. So the audit row says "took 5,000" when the user only had 200 and only 200 was actually deducted. `old_balance`/`new_balance` are correct (they show the real before/after), but the `amount` column is now inconsistent with the actual diff.
**Vulnerability:** Future analytics that sum the `amount` column will overcount deductions. Reconciliation between `economy_log` and `transactions` table will fail because the wallet ledger records `-actual_take` while `economy_log` records the larger `-amount`.
**Impact:** Audit data is wrong. Aggregations break. Forensic analysis is misleading.
**Fix:** Change line 134 to bind `actual_take` instead of `amount`. Or add a second `requested_amount` column and store both.

### OBSERVATION #1: `admin_give`/`admin_take` ref_key timestamps use second granularity — duplicate resolution at 1Hz

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:81, 113`
**Confidence:** 0.7
**Risk:** `int(datetime.now(timezone.utc).timestamp())` produces a Unix epoch second. Two admin actions on the same user within the same second generate identical ref_keys. `flow_wallet.credit/debit` will treat the second as a duplicate and short-circuit, silently no-op-ing the second action. While this is *correct* idempotent behavior on a literal retry, it breaks legitimate back-to-back commissioner actions ("give 1000 then give 1000 within 1 second" → only 1000 given).
**Fix:** Use `time.time_ns()` or `datetime.now(...).timestamp() * 1e6` (microseconds) to get higher resolution. Or include a UUID4 fragment.

### OBSERVATION #2: `_post_audit` has hardcoded emoji + style coupled with rendering

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:284-289`
**Confidence:** 0.55
**Risk:** The `💰 Economy` title and the embed style are hardcoded in `_post_audit`. If the bot is themed differently (or if the project moves to render-card embeds per CLAUDE.md's rendering pipeline rules), this hardcoded path is a divergence from the unified rendering system documented in CLAUDE.md ("All card renders use a single pipeline").
**Fix:** Use a centralized audit helper that picks up theme/colors from `atlas_themes.py`. Low priority but tracks the project's stated direction.

### OBSERVATION #3: `_eco_check_impl` exposes balances of arbitrary members to commissioner

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:516-526`
**Confidence:** 0.5
**Risk:** This is fine — commissioners are explicitly allowed to view any balance. But the lack of audit logging on *check* operations (read-only) means a malicious commissioner could mine balances for surveillance with no trail. Note: the docstring of the cog says "money management" — passive viewing is presumably out of scope, but it's unaudited.
**Fix:** Optional — log every commissioner-initiated balance check to `economy_log` with `action='check'`, `amount=0`. Useful for audit/forensics.

### OBSERVATION #4: `eco_health_impl` SQL hardcodes table names without schema-version check

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:802-812`
**Confidence:** 0.7
**Risk:** Lines 804 and 808-810 directly query `users_table` and `transactions`. The comment on line 803 ("'users_table' is the canonical table name used by flow_wallet") is true *now*, but creates a coupling to flow_wallet's schema. If `flow_wallet.py` ever migrates to plural `users` or splits table layouts, this will silently break with `OperationalError: no such table: users_table`. There's no ImportError soft-fallback.
**Fix:** Move the count + sum SQL into `flow_wallet.get_user_count()` / `flow_wallet.get_recent_net_flow()` so the schema is owned by one module.

### OBSERVATION #5: `setup` function comment mismatch — top-of-file says "All commands are accessed through /boss eco <cmd>" but this file also exports the public `/flow` command

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:1-9, 702-719`
**Confidence:** 0.8
**Risk:** Documentation drift. The module docstring says "All commands are accessed through `/boss eco <cmd>` via boss_cog.py" but the file defines a public `@app_commands.command(name="flow")` at line 702 that *bypasses* boss_cog entirely. Future maintainers reading the docstring will not realize this cog also owns a public slash command.
**Fix:** Update the module docstring to mention `/flow` and the FlowHubView.

### OBSERVATION #6: `INTERVAL_HOURS["monthly"] = 720` is approximate (30 days) — not calendar months

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:32-37`
**Confidence:** 0.85
**Risk:** Monthly stipends fire every 30 days, not on the 1st of each month. Over a year, "monthly" stipends pay 12.17 times not 12. Commissioners might expect calendar-month semantics.
**Fix:** Either rename to `every_30_days` or use `dateutil.relativedelta` for actual calendar-month logic.

### OBSERVATION #7: `_resolve_name` falls back to `User …{last_4_digits}` — not anonymized correctly

**Location:** `C:/Users/natew/Desktop/discord_bot/economy_cog.py:961-966`
**Confidence:** 0.55
**Risk:** When the bot can't resolve a Discord ID (left server, or cache miss), it shows `User …1234` where 1234 is the last 4 digits of the Discord snowflake. Snowflakes are not random — the last digits are timestamp-derived and somewhat correlated. This isn't a serious privacy hole but it's worth knowing that "show last 4" doesn't anonymize like it does with credit-card numbers.
**Fix:** Use a hash of the snowflake (`hashlib.sha256(str(uid).encode()).hexdigest()[:4]`) for genuine pseudonymization, or just say "Departed user".

## Cross-cutting Notes

- **Idempotency story is half-implemented:** `admin_give` / `admin_take` plumb `reference_key` correctly through to `flow_wallet.credit/debit`. `admin_set` does not — `set_balance` has no idempotency layer at all. This same gap likely exists in any other admin tool that uses `set_balance`. Worth a project-wide grep for `set_balance(` calls.
- **`_ctx_*` blanket silent excepts:** The pattern `try: dispatch except discord.NotFound: return` repeated 8 times in FlowHubView with no logging is a copy-paste smell — extract a helper that does logged dispatch with a single Exception → log.exception → graceful fallback message path.
- **Channel-key spelling drift:** `"admin_chat"` vs `"admin-chat"` typo at line 327 should prompt a project-wide audit. Other cogs may have the same typo silently dropping alerts. Consider centralizing channel keys as constants in `setup_cog.py` (e.g. `CHANNEL_ADMIN_CHAT = "admin_chat"`) and importing them everywhere instead of string literals.
- **Defense-in-depth permission check on `_impl` methods:** The pattern of "the slash command checks, the impl trusts" is fragile. The `_ctx_eco_health` callback at line 1083 *does* the check redundantly and is the only one that does — confirming the project intent is "check at every layer". Audit other cogs for the same gap.
- **Stipend math abstraction:** The remainder-distribution logic at lines 369-380 should be extracted into a pure helper `def split_amount(total, num_recipients) -> list[int]` that's unit-testable. Right now it's tangled with the payment loop and impossible to test in isolation.

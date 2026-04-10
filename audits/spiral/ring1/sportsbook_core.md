# Adversarial Review: sportsbook_core.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 645
**Reviewer:** Claude (delegated subagent)
**Total findings:** 18 (3 critical, 8 warnings, 7 observations)

## Summary

The file is structurally disciplined — idempotent reference_keys are present on every wallet credit, a per-event asyncio lock guards `settle_event`, and settlement uses BEGIN IMMEDIATE for claim-once semantics. But three hard failure modes remain: the `settle_event` early-return on a held lock silently drops concurrent settle attempts (including the 10-minute fallback poller), the v7 migration's refund-then-update loop is non-atomic and will double-refund if it crashes mid-table, and `_register_bus_subscription()` is called from `setup_hook` which discord.py re-runs on every RESUME — stacking N duplicate subscribers that cause N parallel settlement races per event.

## Findings

### CRITICAL #1: Duplicate bus subscribers on RESUME / setup_hook re-entry
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:608-623` (plus `bot.py:319-327`)
**Confidence:** 0.88
**Risk:** `_register_bus_subscription()` appends a new lambda handler to `flow_bus._handlers["event_finalized"]` every time it is called. `bot.py` calls it unconditionally inside `setup_hook()`, which discord.py may invoke more than once over the bot's lifetime (the CLAUDE.md specifically calls out `_startup_done` as the standard guard for duplicate `load_all()` on reconnect — this file has no such guard). Additionally, `settlement_poll.start()` on line 324 of bot.py will raise `RuntimeError: Task is already launched...` if setup_hook re-runs, and because the whole block is inside a `try/except Exception`, the exception is swallowed but the duplicate subscribe on line 616 still registered beforehand.
**Vulnerability:** `FlowEventBus.subscribe` has no dedup guard (`flow_events.py:55-56`), and `settle_event` guards concurrent same-event work *only within a process via `_settle_locks`* — but `_settle_locks` returns early with a no-op when locked (line 228-229), so N duplicate subscribers firing on the same payload collapse to one settlement, BUT the same payload also creates N asyncio tasks via `asyncio.create_task` (line 618), each of which separately grabs a wallet credit lock. The idempotency of `flow_wallet.credit(reference_key=...)` saves the money path, but wager_registry's `settle_wager` does not use reference keys — and the update-by-composite-key pattern means the first winner updates the row, the others become no-ops against `status='open'` (wager_registry.py:141). So the wallet is safe, but log noise and CPU waste multiply linearly with reconnects, and the poller task is partially broken.
**Impact:** After the bot survives any network blip, every event settlement races N ways in parallel, burning CPU and DB connections. The poller loop may never run again after the first reconnect (raises on restart, swallowed). Over weeks of uptime this degrades to scores of handlers per event.
**Fix:** Guard `_register_bus_subscription` with a module-level `_bus_subscribed = False` flag; also guard `settlement_poll.start()` with `if not settlement_poll.is_running():`. Ideally move both to a once-per-process call site (e.g., `on_ready` with a `_startup_done` guard) rather than `setup_hook`.

### CRITICAL #2: Non-atomic v7 migration — double refund on mid-migration crash
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:314-394`
**Confidence:** 0.92
**Risk:** `run_migration_v7` does per-row credits → UPDATE status='Refunded' → commit *per table*. If the process is killed, power-loses, or hits an exception between `flow_wallet.credit()` and `UPDATE ... SET status='Refunded'`, the credit is permanent (flow_wallet writes on its own connection) but the row still reads `status='Pending'`. On next startup, the guard check (line 324-327) reads `schema_meta.schema_version` — but `schema_meta` lives in **flow.db**, not **OLD_SB_DB**, and the schema is created on line 393 (`await setup_db()`) *after* the refund loop. So if the migration crashes halfway through the refund loop on first run, flow.db does not yet have `schema_meta`, so the guard at line 327 returns False on retry, and the refund loop re-runs against the OLD_SB_DB rows that are still `status='Pending'` — **every Pending bet refunded during the failed run gets refunded a second time**. The `reference_key=f"MIGRATE_V7_REFUND_{table}_{r[id_col]}"` on line 358 *does* save this from actual financial loss (it's idempotent), but the status update loop continues to no-op because the rows already have `status='Refunded'`… except rows that *were* still Pending at crash time will be re-refunded (reference key unique) and marked Refunded. Medium-probability data path; high-impact if reference_key uniqueness ever drifts.
**Vulnerability:** The guard reads flow.db (line 324) but the mutations happen on OLD_SB_DB (line 342). The two DBs are NOT in a transaction together. The wallet credit writes to yet a third DB (flow_economy.db). There is no durable marker that says "v7 refund phase has started on OLD_SB_DB" until `await setup_db()` completes on line 393 and *implicitly* sets schema_version=7 via the `INSERT OR IGNORE` on line 67. So the window between "first credit issued" and "setup_db() commit" is an unguarded re-run zone.
**Impact:** If idempotency on wallet credits ever has a gap (e.g., reference_key truncation, DB corruption during crash), users are double-paid. Even without a gap, retrying the migration after a crash wastes DB work and re-emits `log.info("refunded N Pending from...")` lines that look like real refunds to anyone reading logs.
**Fix:** Write the schema_version marker *before* touching wallets. Acquire an explicit row in `schema_meta` with value='7_in_progress' at the start of the function, then flip to '7' at the end. Check both values on retry. Or, safer: wrap the entire migration (refund loop + archive rename + schema create) in a single BEGIN IMMEDIATE on OLD_SB_DB so that partial state rolls back. Note that the current code also commits once per table (line 365) so even the OLD_SB_DB state is partially durable mid-loop — use a single commit at the end.

### CRITICAL #3: `settle_event` drops concurrent settle requests silently (lock.locked() early return)
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:227-230`
**Confidence:** 0.85
**Risk:** The lock check is `if lock.locked(): return` — not `async with lock: ...`. This is not a no-op: it is a hard drop of the second caller. Consider this race: bus handler fires (opens tx, reads event as status='final', starts grading bets), and mid-grade the settlement poller loop (line 625) fires for the same event. Poller calls `settle_event(event_id)`, finds the lock held, returns immediately. This is fine *if* the bus handler is guaranteed to settle every bet in the pending list. But the bus handler's pending snapshot was taken on line 246 — any bet inserted *after* that snapshot but before `status='final'` is set (e.g., a retroactive placement via `write_bet` on a final event, or a stale-cache edge) will not be in the snapshot, so both handlers return — bet remains Pending forever until the poller's next run (up to 10 minutes) or until another bus event kicks it. Worse: the pattern is advertised as "concurrent calls are no-ops" in the docstring but the sibling invariant (settle_event idempotent) is achieved via the per-bet `SELECT status FROM bets ... BEGIN IMMEDIATE` guard on lines 273-279 — so the correct behavior is to *await the lock* rather than drop. Additionally, `_settle_locks` is never cleaned up — it's an unbounded dict keyed by event_id, leaking memory across seasons (thousands of games → thousands of Lock objects retained forever).
**Vulnerability:** The "optimization" of early-returning on held lock replaces wait semantics with drop semantics, but the whole settle_event body is already idempotent (per-bet BEGIN IMMEDIATE on line 273). The early return exists to prevent work duplication, but costs correctness for edge cases where the running settlement does not cover the requester's intent. The poller's log line on line 645 says `"settlement_poll error"` only on exception, so dropped calls are invisible.
**Impact:** Edge-case bets remain Pending across polling intervals; users see "settling..." indefinitely. Memory grows unbounded across seasons. Debuggability suffers — no log line records the drop.
**Fix:** Change to `async with lock:` (wait for the other caller). The per-bet idempotency guard on lines 273-279 already prevents double-crediting. Add a `log.debug` inside the lock to track serialization. Add TTL cleanup or weakref for `_settle_locks` (or use `event_id → asyncio.Lock` from `functools.lru_cache` on size).

### WARNING #1: `settle_event` commit-order: wallet credit happens BEFORE bet status flip — crash leaves credit without status update
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:258-283`
**Confidence:** 0.82
**Risk:** The "credit-first two-DB pattern" docstring on line 225 acknowledges this: wallet credit is issued on flow_economy.db via `flow_wallet.credit(...)` at line 261/266, *then* the bet status is flipped on flow.db at line 280-283. If the process is killed between the wallet write and the bet UPDATE, the user has the payout but the bet row still shows `status='Pending'`. On restart, the poller (line 625) or bus (line 615-619) re-enters `settle_event`, grading the same bet again — and re-issues the credit with the same `reference_key=f"BET_{bet_id}_settled"`. The reference_key *does* make this safe (flow_wallet detects dup), so no double-pay. BUT the bet status UPDATE and `wager_registry.settle_wager("BET", ...)` re-execute: wager_registry has no reference_key, and the update on line 141 of wager_registry is scoped to `WHERE ... status='open'`, so the second call no-ops and the wager registry is consistent. The window is small (single wallet write + single SQLite UPDATE), and idempotency saves both sides. But the pattern is fragile: any caller who assumes "credit issued => bet marked settled" (e.g., a stats dashboard that joins bets and transactions) will briefly see a credit without a corresponding settled bet.
**Vulnerability:** The two-DB design means atomicity is impossible; mitigated only by per-step idempotency. If a future developer adds a non-idempotent step between the credit and the bet UPDATE (e.g., a Discord message send that isn't wrapped in try/except), the crash window becomes user-visible.
**Impact:** Monitoring false positives. Possible confusion in reconciliation reports. No actual loss given current idempotency.
**Fix:** Document this invariant prominently at the top of the function and add a regression test that kills the process between credit and bet UPDATE. Consider emitting a "settled" event in wager_registry via reference_key too. Long-term, use a single outbox pattern: write the grade result into flow.db first, then emit a settled event to a worker that does the credit.

### WARNING #2: `settle_event`'s per-bet inner BEGIN IMMEDIATE can rollback AFTER the wallet credit succeeded
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:254-283`
**Confidence:** 0.78
**Risk:** The loop calls `flow_wallet.credit(...)` at line 261/266, then opens a new connection and does `BEGIN IMMEDIATE` at line 273. If another settlement raced in and set the bet status first (line 277-279 `if row and row["status"] != "Pending": await db.rollback(); continue`), the rollback happens *after* the wallet credit succeeded. The wallet credit is idempotent by reference_key, so the duplicate path does NOT double-pay (both paths share reference_key `BET_{bet_id}_settled`), but the current caller *did* effect a credit and then discarded its own DB transaction — the credit is permanent but the bet status is controlled by the winning racer. If the winning racer graded the bet differently (it shouldn't, grade_bet is pure), the credit would not match the declared status. More realistically: if a concurrent caller graded the bet as Push while this caller graded it as Won (e.g., because event_row was mutated between reads — see WARNING #5), we could credit the full payout but the bet is marked Push. Low probability but non-zero given the locking scheme's drop-on-held behavior (CRITICAL #3).
**Vulnerability:** The early-exit on `lock.locked()` in CRITICAL #3 is intended to prevent exactly this race, but with multiple bus subscribers (CRITICAL #1) the lock serializes at most 1 settle_event per event per process — two processes, or two duplicate subscribers inside one process, produce N parallel settle_events that do grade → credit → rollback-on-race, which is O(N) wasted credits (free via idempotency) but also O(N) risk that grade disagreement produces a split brain.
**Impact:** Small risk of "credit issued at W payout, bet status marked P" mismatch if any non-determinism creeps into grade_bet or event_row.
**Fix:** Claim the bet atomically *first* via BEGIN IMMEDIATE + `UPDATE bets SET status='Grading' WHERE bet_id=? AND status='Pending' RETURNING 1`. Only proceed to wallet credit if the claim succeeded. This inverts the current order and makes the DB the single source of truth for "who gets to grade this bet".

### WARNING #3: `run_migration_v7` refund loop f-strings a column name and a table name from a hardcoded list — no SQL injection but dangerous pattern
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:335-365`
**Confidence:** 0.65
**Risk:** Lines 354-355 and 363-364 do `f"SELECT {id_col}, discord_id, {wager_col} FROM {table} WHERE status='Pending'"` and `f"UPDATE {table} SET status='Refunded' WHERE {id_col}=?"`. The values come from the hardcoded REFUND_SOURCES constant (line 335-340), so SQL injection is not exploitable today. But the pattern normalizes f-string SQL construction in a security-sensitive file. Any future developer adding a new entry sourced from config or the network will reproduce the anti-pattern. Also, the table existence check on line 348-349 is subject to TOCTOU: a table named dynamically could be renamed between the existence check and the SELECT.
**Vulnerability:** Constants today; less safe tomorrow.
**Impact:** Invitation for regression.
**Fix:** Use parameterized queries where possible (SQLite doesn't parameterize identifiers, but `quote_ident` or a whitelist validator helps). Or inline the four tables as separate hardcoded SQL blocks rather than a loop over column names.

### WARNING #4: `run_migration_v7` guard swallows ALL exceptions, not just "file doesn't exist"
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:320-331`
**Confidence:** 0.85
**Risk:** The try/except on lines 320-331 is labeled "FLOW_DB may not exist yet" but catches `Exception`. If the flow.db file exists but is corrupted, or if `schema_meta` exists but `row[0]` is non-integer, the exception is swallowed and the migration proceeds as if schema_version < 7 — **re-running the entire refund loop on a live v7 DB**. Refunds are idempotent by reference_key, but all those `MIGRATE_V7_REFUND_{table}_{id}` lookups hammer OLD_SB_DB for a non-bug reason and the "refunded N Pending" log lines mislead ops. Worse: if the OLD_SB_DB has been rebuilt and now contains different bet_ids under the same table name (unlikely but possible if a restore-from-backup was done), new refunds are issued against the new rows. The guard is one line of defense; it should be tight.
**Vulnerability:** Broad `except Exception: pass`. No log. No observability.
**Impact:** Silent re-migration on any DB hiccup; wrong refunds in backup-restore scenarios.
**Fix:** `except aiosqlite.OperationalError: pass` (and maybe `FileNotFoundError`) only. Log any other exception via `log.exception(...)` and re-raise. The `pass` on line 331 also violates the "silent except is prohibited in admin-facing views" rule (CLAUDE.md) — this isn't a view, but it's admin-facing behavior.

### WARNING #5: `settle_event` reads `event` under a connection that's closed before the bet loop runs
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:231-255`
**Confidence:** 0.80
**Risk:** Line 231-247 opens a connection, reads the event and the pending bets, then exits the `async with`. The connection is closed at line 247. Line 252 converts the event Row to a dict, and line 254-296 loops over pending bets, grading each against the dict. In between reading the event and grading the bets, the connection is closed, so concurrent writers to `events` can change scores without this caller noticing — and this caller will happily grade against stale scores, credit wallets based on stale grades, then in the per-bet BEGIN IMMEDIATE block (line 270-283) mark the bet's status. If `finalize_event` is called twice with different scores (e.g., ingestor retry with corrected data), the first settle_event grades with old scores and the second call is a no-op (all bets Pending→Won). The user is paid at the old grade.
**Vulnerability:** Event snapshot is taken once and reused across the entire settlement. No re-read. No guard on event score stability.
**Impact:** Small but real: if Madden data gets corrected, payouts lock to the first version.
**Fix:** Guard `events` with an `immutable_once_final` check, or re-read event inside the per-bet BEGIN IMMEDIATE. Alternatively, refuse to re-write `events.home_score` after `status='final'` (enforce in `finalize_event` with an UPDATE ... WHERE status='final' AND home_score IS NOT NULL guard).

### WARNING #6: `grade_bet` crashes on missing keys with KeyError, callers don't defend
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:98-140`
**Confidence:** 0.72
**Risk:** `grade_bet` accesses `event_row["status"]` (line 98), `bet_row["bet_type"]` (line 101), `event_row["home_participant"]` (line 104), etc. via `[]` — raises KeyError on missing key. The caller (line 256) constructs the dicts from SQLite Row objects via `dict(bet)`/`dict(event)`, so the keys match the schema as long as the SELECT on line 234-237/242-243 matches. But any schema drift or migration that drops a column will crash grade_bet, and the enclosing `settle_event` has no try/except around the loop — so a single bad event crashes the entire settlement, leaves the wallet credit dangling (if it was already issued for a prior bet in the same event), and propagates up to `settlement_poll` which catches it and logs `[CORE] settlement_poll error`. All subsequent pending bets for that event remain Pending until the next poll, and the error has no actionable info beyond "settlement_poll error".
**Vulnerability:** Missing per-bet try/except in the loop (line 254-296). A KeyError or ValueError (e.g., bad JSON on line 134) kills the whole loop.
**Impact:** One bad bet blocks all bets on the same event.
**Fix:** Wrap the per-bet loop body in try/except that logs `log.exception("bet %d grading failed", bet_id)` and marks the bet status='Error', then continues. The bets schema already allows 'Error' status on line 41.

### WARNING #7: `grade_bet` does `pick == winner` but winner can be None if both scores are 0
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:106-113`
**Confidence:** 0.70
**Risk:** `h_score = float(event_row["home_score"] or 0)` — if home_score is None in the DB (which violates the final-event invariant but is possible if `finalize_event` was called with `home_score=None`), this becomes 0.0. If both scores are None → 0==0 → 'Push'. Not strictly wrong for Moneyline, but the Spread branch on line 118 computes `covered = 0 + line - 0 = line`, which treats an unscored game as "you covered by exactly `line`" — a Won if line > 0, a Lost if line < 0. This gives phantom wins/losses on malformed events. The `status=='final'` check on line 239 guards settle_event entry, but nothing enforces that `final` implies non-null scores.
**Vulnerability:** No not-null constraint on `home_score`/`away_score` in the schema (line 28: `home_score REAL, away_score REAL` — nullable). `finalize_event` (line 590) requires the scores to be passed, but int-vs-float type and None-vs-0 confusion remain.
**Impact:** Malformed final events grade bets incorrectly.
**Fix:** Assert scores are non-null inside `settle_event` (raise/skip if `event["home_score"] is None or event["away_score"] is None`). Add `NOT NULL` + `CHECK(home_score IS NOT NULL OR status != 'final')` in the schema (but that's a migration).

### WARNING #8: `write_event`, `write_parlay`, `write_parlay_leg` use INSERT OR IGNORE — silently drops conflicting writes
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:486-549`
**Confidence:** 0.68
**Risk:** Three public writers use `INSERT OR IGNORE`. On a conflict (e.g., duplicate parlay_id), the row is silently not written. The function returns success, callers believe the parlay exists with the values they supplied — but the DB has the OLD values from the first insertion. `write_bet` (line 552) uses `INSERT` without IGNORE, so it errors on FK violation (good), but returns `cur.lastrowid` which is the correct new id. In contrast, `write_parlay` callers never re-fetch the combined_odds they just wrote, so if a re-call is made with updated odds for the same parlay_id (e.g., live line movement), the old odds silently win. Similar for `write_parlay_leg` — if two different bet_ids are written against the same (parlay_id, leg_index), the second silently wins (UNIQUE constraint on line 57), losing the second bet's leg.
**Vulnerability:** `INSERT OR IGNORE` is the right default only if callers know to re-read after the write. No docstring on any of the three functions says "check the DB if you need the canonical value".
**Impact:** Subtle parlay construction bugs where leg data diverges from bet data.
**Fix:** Either document the idempotency contract loudly, or use INSERT ... ON CONFLICT DO NOTHING RETURNING rowid so callers can detect dedup. Or return a boolean "was_inserted" flag.

### OBSERVATION #1: OLD_SB_DB defaults to `flow_economy.db` but the env var is named `FLOW_DB_PATH`
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:16`
**Confidence:** 0.90
**Risk:** `OLD_SB_DB = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))` — the variable is semantically "the old sportsbook DB to migrate away from" but its default path is `flow_economy.db`, which per CLAUDE.md is the live Flow store/wallet/affinity DB, NOT the legacy sportsbook.db. The legacy DB per CLAUDE.md is `sportsbook.db` (orphaned), and the live DB is `flow_economy.db`. So the migration runs refund + table-rename against the LIVE DB (flow_economy.db), not the legacy DB. If `sportsbook.db` ever held the Pending bets that needed refunding (the actual legacy data), they are not being migrated at all. This is either (a) correct-by-convention (the pre-v7 code was already writing to flow_economy.db under different table names), or (b) a misnamed variable that points away from the legacy data. The `FLOW_DB_PATH` env var name is also confusing because `FLOW_DB = flow.db` (line 17) is a different file.
**Vulnerability:** Misleading variable name. Two different DBs live under FLOW_* names.
**Impact:** Naming confusion during operational review.
**Fix:** Rename the variable to `LEGACY_SB_DB` (or `OLD_FLOW_ECONOMY_DB`) and use a dedicated env var (`LEGACY_SB_DB_PATH`). Or add a docstring clarifying the file.

### OBSERVATION #2: Schema is loaded via `executescript` with PRAGMA foreign_keys inside the script, but also set via `await db.execute` before the script runs
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:305-311`
**Confidence:** 0.60
**Risk:** `setup_db` does `await db.execute("PRAGMA foreign_keys=ON")` (line 308) and then `await db.executescript(_SCHEMA_SQL)` (line 309) where the script starts with `PRAGMA foreign_keys = ON`. Double-setting the pragma is harmless but confusing. Also, `executescript` in SQLite implicitly commits pending transactions — harmless here but a gotcha in future edits.
**Vulnerability:** Redundant pragma. Minor.
**Impact:** None functional; just tidiness.
**Fix:** Remove the duplicate `PRAGMA foreign_keys = ON` from either the module constant or the Python call.

### OBSERVATION #3: `schema_meta` row is created with `INSERT OR IGNORE` and default value '7' — there's no way to detect "schema created fresh at v7" vs "migrated to v7"
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:66-67`
**Confidence:** 0.55
**Risk:** A fresh DB created by `setup_db` goes straight to schema_version='7' via the INSERT OR IGNORE. A DB that existed before v7 and was migrated also ends up at '7'. There's no provenance: no `migrated_at`, no `migrated_from`, no `initial_version`. Future migrations that need to know "did we ever go through v7?" can't tell a fresh-install from a migrated DB.
**Vulnerability:** Observability/replay debt.
**Impact:** Future migrations will have to guess.
**Fix:** Add a `created_at` column, or store JSON in `value` with `{"version": 7, "migrated_at": "...", "created_at": "..."}`.

### OBSERVATION #4: `settle_event` log format uses f-string instead of `%s` lazy formatting
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:217, 302`
**Confidence:** 0.50
**Risk:** `log.info(f"[CORE] Parlay {parlay_id} → {final_status}")` and `log.info(f"[CORE] settle_event({event_id}) — graded {len(pending)} bets")` — the f-string is evaluated even when logging is below INFO level. Cheap here but a convention violation compared to other files.
**Vulnerability:** Style.
**Impact:** Negligible.
**Fix:** Use `log.info("[CORE] Parlay %s → %s", parlay_id, final_status)`.

### OBSERVATION #5: `_post_settlement_card` embeds user-controlled participant strings directly in the matchup label
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:432-436`
**Confidence:** 0.50
**Risk:** `home` and `away` come from `event.get("home_participant", ...)` which in turn comes from whatever ingestor wrote the event. If an ingestor ever allows user-supplied strings (e.g., custom prediction market), the name can contain Discord markdown (**, __, backticks, pings like `@everyone`) that render in the embed. The matchup label uses `**...**` which is safe Markdown but does not escape user content. The `away @ home` format with `@` in "away @ home" is a benign mention trigger — Discord treats `@{non-mention}` as text, but `@everyone` / `@here` in a team name would ping. Unlikely for TSL / NFL data, possible for Polymarket titles.
**Vulnerability:** No escaping of AllowedMentions or backtick stripping.
**Impact:** Potential ping injection from event payload.
**Fix:** Pass `allowed_mentions=discord.AllowedMentions.none()` on `channel.send` (line 479). Escape or truncate participant names.

### OBSERVATION #6: `settlement_poll` has no startup wait — runs immediately on `start()`
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:625-645`
**Confidence:** 0.55
**Risk:** `@tasks.loop(minutes=10)` with `settlement_poll.start()` on first call runs the loop body immediately, then every 10 minutes. The first run happens during `setup_hook` startup when the bot may not yet be logged in (`_bot.get_channel` will return None, `_post_settlement_card` gracefully handles it, but the log line warns about "channel not in cache"). Cosmetic noise.
**Vulnerability:** Slightly premature execution.
**Impact:** Log spam on boot.
**Fix:** Add `@settlement_poll.before_loop: await bot.wait_until_ready()`.

### OBSERVATION #7: `_check_parlay_completion` does not log the wager_registry settle_wager outcome and cannot detect write failure
**Location:** `C:/Users/natew/Desktop/discord_bot/sportsbook_core.py:216`
**Confidence:** 0.45
**Risk:** `wager_registry.settle_wager(...)` is fire-and-forget. Its update is scoped to `WHERE ... status='open'`, so if the wager was already settled (e.g., duplicate handler from CRITICAL #1), the UPDATE no-ops silently. No log line records whether the wager was actually settled. This is internal consistency debt.
**Vulnerability:** Observability gap.
**Impact:** Reconciliation questions post-incident.
**Fix:** Capture the rowcount or return value and log it.

## Cross-cutting Notes

- **Two-DB credit-first pattern** is used consistently in both `settle_event` and `_check_parlay_completion`. The idempotency floor is held up entirely by `reference_key` passed to `flow_wallet.credit` — this is good, but wager_registry has no such key, so it relies on its `WHERE status='open'` guard. Any future change that broadens wager_registry updates to include non-open states needs to preserve this guard.
- **The "Legacy orphan"** label from CLAUDE.md is that `sportsbook.db` is orphaned. This file instead defaults OLD_SB_DB to `flow_economy.db` (the live DB). Cross-check: `flow_sportsbook.py`, `real_sportsbook_cog.py`, and `polymarket_cog.py` all `import sportsbook_core` and call its write APIs — they write to `flow.db` (a third DB), so the migration's refund-source table list (`bets_table`, `real_bets`, `prediction_contracts`, `prop_wagers`) is a list of OLD schema tables that lived on flow_economy.db before v7. This is self-consistent with the naming but confusing. Recommend auditing the other three callers to confirm they correctly target flow.db for all writes post-v7.
- **Discord.py task lifecycle**: `settlement_poll.start()` is called from setup_hook. setup_hook may re-run under reconnect semantics in some discord.py versions. Combined with CRITICAL #1, the reconnect path is fragile. Recommend adding `_startup_done` guard per CLAUDE.md convention (called out as a gotcha in the file's load-order section) to every startup-bound registration: bus subscribe + task start + migration guard.
- **`_settle_locks` memory leak** (CRITICAL #3) is symptomatic: any long-lived dict keyed by a monotonic identifier needs a cleanup strategy. Consider `weakref.WeakValueDictionary` or an LRU with size cap.

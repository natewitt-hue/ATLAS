# Adversarial Review: real_sportsbook_cog.py

**Verdict:** block
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 904
**Reviewer:** Claude (delegated subagent)
**Total findings:** 22 (4 critical, 10 warnings, 8 observations)

## Summary

This file is the entry point for real-money (TSL Bucks) wagers on live ESPN events and is the highest-risk module in the Flow ring. It contains a confirmed financial idempotency bug (the primary `flow_wallet.debit` for every real bet has no `reference_key`), a refund-write idempotency hole that can double-credit on retry, the *promised* auto-grade pipeline never starts because the background task is never `.start()`-ed, and the lock task degenerates to "lock everything" the moment ESPN returns an empty `event_date`. This file should not ship to production until at least the four critical findings are addressed.

## Findings

### CRITICAL #1: `flow_wallet.debit` for primary bet is missing `reference_key` — every Discord retry double-debits

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:778-781`
**Confidence:** 0.98
**Risk:** The primary financial debit on every real-sport bet is called WITHOUT a `reference_key`. CLAUDE.md is explicit: "ALL debit calls MUST pass `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits."
**Vulnerability:** `flow_wallet.debit` (verified in `flow_wallet.py:277`) accepts `reference_key` as a parameter; if it is omitted, every retry from Discord (which is *common* for modal submissions and button presses, especially under network blips) will produce a fresh ledger row. The downstream `_send_err`/refund branches at L808-818 only fire on `write_event/write_bet` failure — they cannot detect a second debit that already cleared. The refund call at 810 *does* pass a `reference_key`, but that one is only fired on the unhappy path; the duplicate-charge happy-path bug still occurs because the original debit wasn't keyed.
**Impact:** A user who clicks the wager button while Discord retries the interaction (timeout/race) will be charged 2× the wager but get only 1 bet. This is the exact scenario CLAUDE.md flags as the #1 hard-won lesson of the Flow subsystem. Direct corruption of `flow_economy.db` ledger and user balance.
**Fix:** Mirror the refund-key format used at L813:
```python
new_balance = await flow_wallet.debit(
    uid, amt, "REAL_BET",
    description=f"Bet: {pick} ({bet_type})",
    reference_key=f"REAL_BET_DEBIT_{uid}_{espn_event_id}_{int(time.time())}",
)
```
…but ideally key it to a value that is stable across Discord retries (e.g., `f"REAL_BET_DEBIT_{uid}_{espn_event_id}_{interaction.id}"`) so a true retry collapses to the same ledger row instead of creating two near-simultaneous rows with different timestamps.

---

### CRITICAL #2: Lock task locks every event with empty `commence_time` because the upsert default is `""`

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:382-387` (lock SQL) interacting with `:501` (`game.get("event_date", "")`)
**Confidence:** 0.93
**Risk:** The odds upsert defaults `commence_time` to the empty string `""` when ESPN returns no `event_date`. Sixty seconds later the lock task executes `WHERE locked = 0 AND completed = 0 AND commence_time <= ?` with `?` bound to the current ISO timestamp. SQLite string comparison: `'' <= '2026-04-09T...'` → True. Every event without a commence time is silently locked, killing all bet placement on it.
**Vulnerability:** `_parse_commence("")` at L770 also returns `None`, so `_place_real_bet` would have allowed the bet through its commence-time check at L771 — but the row is `locked=1` already, so the user gets "This game is already locked." with no explanation. The user-facing UX is "ESPN failed → all games appear locked," and there is no log line tying it back. Compounding: any **already-placed** pending bet on the event becomes mid-game-locked even though the game hasn't started.
**Impact:** Quietly disables the entire sportsbook for any sport whose ESPN feed is missing event dates (NBA, MMA, soccer were specifically called out at L519 as inconsistent). Users see locked events and no recourse.
**Fix:** (a) Skip rows with empty commence_time in the upsert: `if not game.get("event_date"): continue` (or set the schema column to `NOT NULL`). (b) Add `AND commence_time != ''` to the lock task WHERE clause. (c) Default `commence_time` to NULL in the upsert and rely on SQLite's `NULL <= ?` evaluating to NULL (false) so the row is never matched.

---

### CRITICAL #3: Auto-grade pipeline never runs — `sync_scores_task` is declared but never `.start()`-ed

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:343-358` (cog_load/cog_unload) vs `:364-394` (task definitions)
**Confidence:** 0.97
**Risk:** `cog_load` only starts `self.lock_started_games.start()`. Both `sync_scores_task` (every 10m) and `sync_odds_task` (every 15m) are *declared* via `@tasks.loop` but never started. The class docstring at L9-11 advertises "Score sync: every 15 minutes (all sports)" and "Lock check: every 60 seconds." The score loop's docstring (L366) says "auto-grade completed bets." None of that runs.
**Vulnerability:** `cog_unload` then unconditionally calls `.is_running()` and `.cancel()` on these dead loops (L353-357), which is safe but is also the developer's evidence that the symmetry was supposed to exist — somebody removed `.start()` from `cog_load` but didn't remove the unload-side cancel. Any settlement, finalization, or balance credit on a winning bet will not happen until a commissioner manually runs `/boss Sportsbook → Sync All` or invokes `grade_impl`. Pending bets accumulate forever; users do not see payouts.
**Impact:** Silent service failure — a real wager that wins will be flagged as "Pending" indefinitely. The user sees their bet stuck. Settlement, EVENT_FINALIZED emit, and the `flow_live_cog` recap pipeline are all bypassed. This is the entire **value loop** of the cog being inert.
**Fix:** Either add the missing starts in `cog_load`:
```python
self.sync_scores_task.start()
self.sync_odds_task.start()
```
…or, if the suppression at L347-348 ("manual-only to avoid burning API quota during dev restarts") is intentional in production, then the docstring at L8-11 needs to be corrected and the dead `cog_unload` cancels removed for clarity. The current state is the worst of both: documentation lies and dead code in unload.

---

### CRITICAL #4: `_place_real_bet` has TOCTOU between odds-display lock check and `flow_wallet.debit`, AND between debit and `write_event/write_bet`

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:747-808`
**Confidence:** 0.85
**Risk:** Three independent `aiosqlite.connect(DB_PATH)` calls on the hot path: (1) row lookup at L747-754, (2) `flow_wallet.debit` at L778, (3) `sportsbook_core.write_event/write_bet` at L791-806 against `flow.db`. Between (1) and (2) the `lock_started_games` task can flip the row to locked (it runs every 60s and operates on the same DB without coordination). Between (2) and (3) the debit has cleared on `flow_economy.db` but the bet has not been recorded; if the process crashes between L781 and L791, money is gone with no record.
**Vulnerability:** The refund branch at L807-818 only catches *exceptions* from `write_event/write_bet`. It does NOT catch:
- A process kill / OOM between the debit and the writes
- A successful `write_event` and a failed `write_bet` (the refund description says "bet record write failed" but the event row is now stale orphan in `flow.db`)
- A re-entrant retry of `_place_real_bet` from a Discord interaction retry — the L778 debit goes through twice, the L791 writes go through twice, you now have 2 bet rows for the same pick, charged twice (compounds CRITICAL #1).

The check at L771 (`commence_time <= now + 5m`) is a *display-time* check; nothing re-validates the live odds at commit time. If the line moves between display and submit, the user gets locked into stale odds without knowing it. ESPN-side line movement is exactly what oddsmakers reposition for around game start.
**Impact:** Lost user funds, orphaned events, mismatched ledger and bet records, double-bookings.
**Fix:** Wrap the entire path in a single `aiosqlite` transaction with an `INSERT … RETURNING` style commit. Re-fetch *and re-lock* the event row inside that transaction with `BEGIN IMMEDIATE`. Pass an idempotent `reference_key` to the debit (see CRITICAL #1). Snapshot the live odds tuple at display time and reject the bet at submit if `(odds, line)` no longer matches the row.

---

### WARNING #1: `lock_impl` and `void_impl` are commissioner-facing but have no commissioner permission check

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:656-711`
**Confidence:** 0.9
**Risk:** `lock_impl` (locks an arbitrary event by ID) and `void_impl` (refunds *all* pending bets on an arbitrary event ID) are exposed as public methods on the cog. Per CLAUDE.md these are intended to be called from `boss_cog`, but there is no decorator or in-method `is_commissioner()` guard. If any other code path or future delegation forgets the gate at the boss_cog edge, an arbitrary user could trigger arbitrary refunds.
**Vulnerability:** `void_impl` is a destructive financial operation (it credits user wallets and irreversibly marks bets `Void`). Defense in depth says it should self-check, not just trust the caller.
**Impact:** Permission bypass risk; not exploitable today but one renaming away from being so.
**Fix:** Add `if not is_commissioner(interaction.user): return await interaction.followup.send("…", ephemeral=True)` at the top of every `_impl` method, even though the boss_cog wrapper also checks.

---

### WARNING #2: `_get_max_bet` swallows all exceptions (silent except in admin-touched path)

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:77-87`
**Confidence:** 0.9
**Risk:** `try: ... except Exception: return DEFAULT_MAX_BET`. CLAUDE.md is explicit: silent `except Exception: pass` (and `except Exception: return default`) in admin-facing paths is prohibited. This is the *only* throttle on real-money wager size — if the DB throws (locked, schema corruption, file gone), the cog silently falls back to a hardcoded 5000 cap with NO log line.
**Vulnerability:** A commissioner who has lowered the per-event max via the settings table to e.g. `100` will see the cap silently revert to 5000 the moment the DB hiccups. The whole purpose of the admin override defeated, with no audit trail.
**Impact:** Silently larger wagers than the commissioner authorized. Hard to detect.
**Fix:** `except sqlite3.OperationalError: log.exception(...); return DEFAULT_MAX_BET` and narrow the exception. Log every fallback. Optionally fail-closed (refuse the bet) instead of fail-open (5000).

---

### WARNING #3: `void_impl` continues iteration on per-bet refund failure but commits anyway, creating partial-refund inconsistency

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:680-703`
**Confidence:** 0.85
**Risk:** Inside a `BEGIN IMMEDIATE` transaction, the loop catches each per-bet refund exception with `continue`, but never marks the failed bet for retry, never rolls back, and proceeds to commit. The event is then marked `locked=1, completed=1` regardless. Failed refunds become permanently orphaned: the bet is still `status='Pending'`, the event is `completed=1`, no future settlement runs against it, and the user is silently shorted.
**Vulnerability:** The error message "Voided event `…`. Refunded **N** bets." misleads the commissioner — partial refunds look like full refunds because the success message reports `refunded` as the count of successful credits, not the total bets attempted. The commissioner has no way to tell some bets failed without tailing logs.
**Impact:** Real users get short-changed on voided events with no admin-visible signal.
**Fix:** Either fail the entire void atomically (raise on first refund failure, rollback) or track failures, surface them in the response (`Refunded N/M; failed: [bet_id, …]`), and mark failed bets in a `status='RefundFailed'` state for follow-up.

---

### WARNING #4: `_sync_odds` opens a single sqlite connection and iterates *N* games inside a `for` loop with awaited `client.get_game_odds` per ESPN per-game backfill — serializing N HTTP calls under one transaction

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:520-552`
**Confidence:** 0.8
**Risk:** The backfill loop at L526-542 awaits `self.client.get_game_odds(eid, league_key)` once per nulled game serially. With 16 NFL games per Sunday, this can take 30+ seconds (no per-call timeout). The odds task runs every 15 minutes, but if backfill regularly exceeds the inter-call gap, the loop overlaps with the next loop tick (no `_inflight` guard). On NBA nights with 14 games and a chatty endpoint, this can cause runaway connection growth.
**Vulnerability:** The outer `await asyncio.wait_for(..., timeout=10.0)` at L429 only protects the scoreboard call, NOT the per-game backfill at L527. The per-game `get_game_odds` is unbounded.
**Impact:** Cog appears to hang during odds sync; new sync starts firing while the old one is still running. The aiosqlite pool can be exhausted (each backfill block reopens a connection at L544 every minute). Possible odds-sync drift behind real game times.
**Fix:** Wrap `get_game_odds` in `asyncio.wait_for(..., timeout=5.0)`, run backfill calls concurrently with `asyncio.gather(*[…], return_exceptions=True)`, and short-circuit if the loop is already running (check via a class-level `_odds_inflight: bool` flag).

---

### WARNING #5: `_sync_scores` swallows finalize exceptions per-game and continues to commit other games — but never re-marks the event for retry

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:603-622`
**Confidence:** 0.8
**Risk:** When `sportsbook_core.write_event` / `finalize_event` / `flow_bus.emit` throws, the exception is logged but the `real_events` row is *already* updated to `completed=1` at L595-601 BEFORE the finalize block runs. So next time `_sync_scores` runs, the row still says `completed=1` and the bot skips the finalize step entirely (the `if completed:` branch fires only once). Result: the event row in `real_events` is marked complete, but `flow.db` (sportsbook_core) never received the finalize event and bets stay Pending forever. No retry mechanism.
**Vulnerability:** The `_sync_scores` loop has no idempotency check on the sportsbook_core side — it only relies on the `if completed:` gate against the local table state, which is set BEFORE the cross-DB write.
**Impact:** Permanent settlement loss for any game where a sportsbook_core write throws once. Users have winning bets that stay Pending forever (unless a commissioner manually re-grades, and even that only fires the `_sync_scores` loop, which will skip the row again).
**Fix:** (a) Check `sportsbook_core` for whether the event is already finalized before skipping. (b) Defer the `completed=1` write to *after* the finalize block succeeds. (c) Add a `finalized_at` column on `real_events` and set it only on successful finalize, then the loop key off of `finalized_at IS NULL`.

---

### WARNING #6: `_evaluate_bet` is dead code — never invoked anywhere in the cog

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:102-150`
**Confidence:** 0.9
**Risk:** Module-level function declared "for reuse" (per the comment at L100), but `Grep` confirms it has zero call sites in this file. Bet settlement is delegated to `sportsbook_core.finalize_event`. The local `_evaluate_bet` is a stub that lies about being authoritative — its rules diverge from any settlement logic in `sportsbook_core`.
**Vulnerability:** Future maintainer reads the function and assumes it is the source of truth, modifies it, and the change has no effect. Worse: someone wires it in as a "quick fix" and silently corrupts settlement, because its tie-handling for spread (`adjusted == away_score → Push`) and its under/over logic are not co-validated with `sportsbook_core`. Also note `bet_type == "Spread"` here — but `pick` carries either `home_team` or `away_team` strings — what guarantees does that have when `home_team` casing differs from what was stored at bet placement time?
**Impact:** Maintenance hazard. Possible silent settlement bug if ever wired up.
**Fix:** Delete `_evaluate_bet` and the comment at L100, or move it to `sportsbook_core` with a proper test harness if it's actually needed there.

---

### WARNING #7: Reference key for refund-on-write-failure includes only `espn_event_id` not the bet attempt — repeated failures collide

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:813`
**Confidence:** 0.7
**Risk:** `reference_key=f"REAL_BET_WRITE_FAILED_{uid}_{espn_event_id}"` does not include a timestamp, attempt id, or wager amount. If the user retries the same bet on the same event and it fails again, the second refund call will be deduped by `flow_wallet.credit` (assuming dedup-by-key is honored) and the user gets ONE refund for TWO failed bets — effectively losing the second wager.
**Vulnerability:** The CLAUDE.md format spec is `f"{SUBSYSTEM}_DEBIT_{uid}_{event_id}_{int(time.time())}"` — it explicitly includes the timestamp for exactly this reason. The cog half-followed the convention.
**Impact:** Silent loss of refund on rapid retry; very hard to detect.
**Fix:** Append `_{int(time.time())}` (or, better, the bet's intended attempt id derived from `interaction.id`) to make the key unique per attempt. Same fix should apply to the debit key from CRITICAL #1.

---

### WARNING #8: `_sync_odds` wraps `client.get_upcoming_odds` in 10s timeout but logs the timeout and returns silently — no admin notification, no retry

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:428-442`
**Confidence:** 0.7
**Risk:** ESPN timeouts and 5xx are logged at ERROR level but the function returns normally. There is no in-cog tracking of repeated failures, no admin notification (`ADMIN_CHANNEL_ID` is unused), no exponential backoff, no circuit breaker. A multi-hour ESPN outage will produce log spam and silently degrade the entire sportsbook UX (stale odds, no new events) with zero escalation.
**Vulnerability:** Operationally invisible failure mode. The score sync (L558) has the same shape.
**Impact:** Silent degradation; commissioners may not notice that ESPN has been down for an entire game day until users complain.
**Fix:** Track consecutive failures on the cog object; on the 3rd consecutive failure, post to `ADMIN_CHANNEL_ID` (if configured). Log at ERROR after first failure, WARNING-with-context after recovery.

---

### WARNING #9: `_migrate_schema` runs DDL on every cog load — race during multi-process startup

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:232-341`
**Confidence:** 0.7
**Risk:** `_migrate_schema` is called from `_setup_tables` which is called from `cog_load`. If two ATLAS processes (e.g. dev and prod, or two replicas, or a manual test against the same `flow_economy.db` file) start simultaneously, both will execute the `ALTER TABLE … ADD COLUMN` and `DROP TABLE real_odds` statements concurrently, racing on the SQLite write lock. SQLite serializes writes but the loser will see "duplicate column" errors and the migration will partially complete then crash.
**Vulnerability:** No `_startup_done` guard around migration, no version table, no idempotency check on the `DROP TABLE real_odds` step (it's wrapped in an "if exists" check, but between the check and the drop there is no transaction holding the schema lock).
**Impact:** Migration hazard on multi-process restarts (which CLAUDE.md hints at because the user explicitly mentions running multiple Claude Code sessions). Half-applied schemas are very hard to recover from.
**Fix:** Wrap the entire migration in `BEGIN IMMEDIATE` transaction. Or add a `schema_version` row in `sportsbook_settings` and skip the migration if version >= target. Or run the migration only once via a `_migrated` class flag.

---

### WARNING #10: `CustomRealWagerModal.on_submit` does not catch `ValueError` from oversized integer parsing

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:855-869`
**Confidence:** 0.65
**Risk:** `int(self.amount_input.value.replace(",", "").replace("$", ""))` catches `ValueError`. But if a user enters something like `1e9` or `0x10`, Python's `int()` will reject it. More importantly, if user enters `9999999999999999999999`, Python parses it fine (arbitrary precision), and the comparison against `max_bet` will catch it — but the int gets passed to the SQLite-bound parameter at L805/L778, where SQLite stores it as INTEGER (signed 64-bit). Values above 2^63 will silently overflow. Also: negative values pass the `< MIN_BET` check (50) only if the comparison short-circuits, but if user enters `-100` it fails the MIN_BET check, fine — but `0` passes through to `flow_wallet.debit` which may or may not handle it.
**Vulnerability:** `int(...)` is too permissive. The label says `Min ${MIN_BET}` but the code only enforces `>= MIN_BET`, not `> 0` or `<= 2^63`. The placeholder doesn't tell users not to use scientific notation.
**Impact:** Edge case, but a malicious or curious user can probe for overflow behavior. Low impact, real risk.
**Fix:** Validate via regex `^\d+$` after stripping commas/dollar signs, then `int()`. Reject overflow explicitly. Document the input format.

---

### OBSERVATION #1: `random.uniform(5, 15)` jitter at the top of every loop body — good idea, but applied AFTER `before_loop` which already waits for ready

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:367, 399`
**Confidence:** 0.6
**Risk:** Stylistic. The jitter helps stagger fleet-wide loads, but inside the `tasks.loop` body it applies on every tick, not just startup. So every 10/15 minutes, the score sync waits an extra 5-15s before doing anything — that's fine, but the comment doesn't explain the intent and a future maintainer may "optimize" it out.
**Fix:** Add a comment: `# Jitter to avoid coordinated load with other cogs across replicas.`

---

### OBSERVATION #2: `random` module imported but only used for jitter — surface that

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:21, 367, 399`
**Confidence:** 0.4
**Risk:** Trivial. Importing `random` for two `uniform` calls is fine, but if you replace it with `asyncio.sleep(uniform(5, 15))` from `secrets` or move to async-friendly jitter you can drop the import.
**Fix:** Optional. Leave as-is.

---

### OBSERVATION #3: `_evaluate_bet` (dead code) treats spread tie as Push but Push is sometimes Lost in real sportsbooks for half-point spreads — doesn't matter because dead

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:116-130`
**Confidence:** 0.3
**Risk:** Dead code (see WARNING #6) but worth flagging that the logic is incomplete: real sportsbooks treat any non-half-point line as susceptible to push, but the function does not validate that `line` is integer vs half-point.
**Fix:** Delete the function (see WARNING #6).

---

### OBSERVATION #4: `cog_unload` cancels `sync_scores_task` and `sync_odds_task` even though `cog_load` never started them — dead defensive code

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:352-358`
**Confidence:** 0.8
**Risk:** Reads as if the symmetry exists ("we start in `cog_load`, we stop in `cog_unload`") but the start lines were removed from `cog_load` (see CRITICAL #3). This is the artifact of an incomplete deletion.
**Fix:** Either restore the starts (preferred — see CRITICAL #3) or remove the dead cancel calls.

---

### OBSERVATION #5: `_setup_tables` and `_migrate_schema` log at INFO but the migration only runs once — no version-pinning trail

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:230, 271, 341`
**Confidence:** 0.5
**Risk:** "Migration: added 4 new columns" gives no indication of *which* DB was migrated or *when*. With multiple ATLAS instances against the same file, a single migration log line is hard to correlate. There is no `schema_version` tracking.
**Fix:** Stamp `sportsbook_settings` with `schema_version=N` after migration, log it, and use it as the gate for future migrations.

---

### OBSERVATION #6: `_place_real_bet` has `source_label` parameter that is never actually used inside the function

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:739, 876-879`
**Confidence:** 0.95
**Risk:** `source_label` is passed in by `CustomRealWagerModal.on_submit` (computed at L871-874) but the body of `_place_real_bet` never references it. Dead parameter. Could be a half-finished refactor where the source label was meant to be used in the description string or for routing.
**Fix:** Either use it in the debit description (e.g. `description=f"Bet: {pick} ({bet_type}) [{source_label}]"`) or remove the parameter from the signature.

---

### OBSERVATION #7: Per-game backfill at L526 does not enforce a per-call timeout

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:527`
**Confidence:** 0.8
**Risk:** See WARNING #4 for the runaway-loop concern. As an observation: every individual `get_game_odds` call has no `wait_for` guard, only the outer scoreboard fetch does. ESPN endpoint hangs would cascade into multi-minute stalls per sport.
**Fix:** Add `asyncio.wait_for(self.client.get_game_odds(eid, league_key), timeout=5.0)` and skip on timeout.

---

### OBSERVATION #8: SPORT_SEASONS configures NCAAB to end in April but March Madness extends into early April — minor calendar mismatch

**Location:** `C:/Users/natew/Desktop/discord_bot/real_sportsbook_cog.py:64`
**Confidence:** 0.5
**Risk:** `"basketball_ncaab": {"months": {11, 12, 1, 2, 3, 4}}` is correct for the regular season. NCAA championship is typically early April, so April 4 is included; this is fine. WNBA at L68 (`{5,6,7,8,9}`) excludes the playoffs which extend into October — could miss late-season odds sync. This is a minor calendar nit.
**Fix:** Extend the windows or accept the gap.

---

## Cross-cutting Notes

Two patterns in this file are likely worth checking across the rest of the Flow ring (Batch B):

1. **Reference key omission on debits**: If the primary `_place_real_bet` path drops `reference_key`, the same author may have made the same omission in `flow_sportsbook.py`, `casino/`, or any other money-out path. Recommend a project-wide grep `flow_wallet.debit\(` and audit every call site for `reference_key=`. The CLAUDE.md hard rule has no exceptions and this file proves the rule is not enforced anywhere mechanically (no linter, no type signature requirement, no decorator).

2. **Background task starts vs. unloads**: The pattern of declaring `tasks.loop` decorators but never starting them in `cog_load` while still cancelling them in `cog_unload` is the kind of footgun that survives because the unload cancel is a no-op (`is_running()` is False). Worth checking other Flow cogs for the same shape — any cog whose value loop depends on a background task should be checked for the start call.

3. **Auto-grade pipeline**: If `real_sportsbook_cog` settlement only runs on manual trigger, `flow_live_cog`'s downstream `EVENT_FINALIZED` subscriber will only fire on manual sync. The settlement-driven Pulse/Recap renders may be permanently inert from a real-sportsbook perspective. Worth grepping for any subscriber that assumes EVENT_FINALIZED is emitted reliably.

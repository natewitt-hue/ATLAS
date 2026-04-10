# Adversarial Review: db_migration_snapshots.py

**Verdict:** block
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 154
**Reviewer:** Claude (delegated subagent)
**Total findings:** 9 (2 critical, 4 warnings, 3 observations)

## Summary

Standalone synchronous migration utility with multiple high-impact issues. `take_daily_snapshot()` is called via `asyncio.to_thread()` from `flow_sportsbook.py:3111` per CLAUDE.md, but the `backfill_from_bets()` function has math that diverges from `odds_utils.payout_calc()` (different truncation pattern) and does NOT handle `Push` bets despite querying them, causing silent data loss in historical sparklines. `setup_snapshots_table()` has ordering dependency on `users_table` existing (which is created by `flow_sportsbook.py`) and no guard for that. All-or-nothing transaction boundary means a crash mid-loop leaves partial snapshots.

## Findings

### CRITICAL #1: `backfill_from_bets()` queries `Push` bets but never handles them — walking history is wrong
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:79-104`
**Confidence:** 0.95
**Risk:** Line 80 filters `status IN ('Won', 'Lost', 'Push')`. The walker then branches on `status == 'Won'` (subtract profit) and `status == 'Lost'` (add back wager). There is NO branch for `'Push'`. Push bets return the wager unchanged — so walking backwards, a push should leave `running` unchanged (correct), but because they're included in the query, each push row overwrites `snapshots[day] = running` with whatever `running` is AFTER the previous iterations, then falls through the if/elif chain doing nothing. This is coincidentally "correct" for the running balance (no change) but it *does* overwrite the snapshot for that day, possibly with a stale value from a later-in-time bet on the same day.
**Vulnerability:** The logic is "seed snapshots[day] to running BEFORE applying the reverse op" — but because we iterate DESC (newest first), the first write for a day wins (`if day not in snapshots`). That's the snapshot of the LATEST balance on that day before reversing. For Push bets, we seed but don't reverse, meaning the snapshot for that day is the post-bet balance, not the pre-bet balance. Pushes mid-day then silently skew the day's recorded balance upward or downward by any intra-day win/loss that preceded them (in chronological order = came after in DESC order).
**Impact:** Financial history corruption in sparklines. Users see incorrect balance trajectories for any day that had a Push bet.
**Fix:** Either exclude Push from the query (`status IN ('Won', 'Lost')`) since they don't move the balance, or explicitly add `elif status == 'Push': pass` with a comment explaining the no-op, AND apply the reverse BEFORE seeding `snapshots[day]` for non-Push rows so the seed is the pre-bet balance.

### CRITICAL #2: Walking history math diverges from `odds_utils.payout_calc()` — will not reconstruct ledger exactly
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:97-102`
**Confidence:** 0.9
**Risk:** The profit math here is `int(wager * odds / 100)` for positive odds and `int(wager * 100 / abs(odds))` for negative. `odds_utils.payout_calc()` uses `int(wager + wager * (odds / 100))` and `int(wager + wager * (100 / abs(odds)))` then subtracts wager. These are NOT the same due to `int()` truncation order:
- For `wager=101, odds=+150`: `int(101 * 150 / 100) = int(151.5) = 151` (here).
- `profit_calc`: `int(101 + 101 * 1.5) - 101 = int(252.5) - 101 = 252 - 101 = 151` (same, OK).
- For `wager=33, odds=-110`: here: `int(33 * 100 / 110) = int(30.0) = 30`.
- `profit_calc`: `int(33 + 33 * 0.909...) - 33 = int(63.0) - 33 = 30` (same).
- But for `wager=100, odds=-110`: here: `int(100 * 100 / 110) = int(90.909...) = 90`; `profit_calc`: `int(100 + 90.909...) - 100 = int(190.909) - 100 = 190 - 100 = 90` (same).
- Edge case: `wager=1, odds=+500`: here: `int(1*500/100) = 5`; `profit_calc`: `int(1 + 5.0) - 1 = 5`. Same.

Most cases match, but the danger is that they are SEPARATE implementations of the same math, so any future edit to `payout_calc` (e.g., switching to `round()` to fix the critical in the `odds_utils.py` review) will NOT propagate here. The backfill will then silently diverge from live settlements.
**Vulnerability:** Duplicated financial math with no shared source. Future fix to `odds_utils` silently breaks backfill consistency.
**Impact:** Sparkline drift over time as the ledger and backfill diverge. Invisible until a user reconciles.
**Fix:** Import `from odds_utils import profit_calc` and replace the inline math with `profit = profit_calc(wager, odds)` on line 97-101.

### WARNING #1: `take_daily_snapshot()` has no error handling — one bad row kills the whole snapshot
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:49-64`
**Confidence:** 0.9
**Risk:** The loop `for discord_id, balance in users:` has no try/except. If any single row has a NULL `discord_id` or a `balance` that fails the INTEGER constraint (e.g., a float that snuck in via casino math per CLAUDE.md "Float vs int balance corruption"), the entire `INSERT OR REPLACE` raises, the `con.commit()` on line 63 never runs, and the context manager rolls back — losing ALL snapshots for the day. Worse, the `print(f"[ATLAS] Daily snapshot taken for {len(users)} users")` on line 64 uses the pre-iteration count, so logs claim success even on partial/failed runs.
**Vulnerability:** Atomicity-vs-logging mismatch. The `with` block rollback is correct for consistency but the log lies on failure.
**Impact:** Silent snapshot loss on any bad row; logs misleadingly claim success. Per CLAUDE.md attack surface: "observability gaps that would hide failure or make recovery harder."
**Fix:** Either wrap the loop in try/except per-row (log and continue), or raise the exception out of the `with` block so the log line is not reached on failure. Also consider: `print(f"[ATLAS] Daily snapshot: {actual_inserted}/{len(users)} users")`.

### WARNING #2: `setup_snapshots_table()` has ordering dependency on `users_table` existing
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:30-46`
**Confidence:** 0.8
**Risk:** The snapshot table is created fine, but `take_daily_snapshot()` on line 56 queries `SELECT discord_id, balance FROM users_table`. That table is created by `flow_sportsbook.py:164-169` during cog load. If `take_daily_snapshot` runs BEFORE `flow_sportsbook._setup_flow_db()` (e.g., from the `__main__` block on line 148, or a cron-run before the bot fully starts), it raises `sqlite3.OperationalError: no such table: users_table`. There is no `CREATE TABLE IF NOT EXISTS users_table` here.
**Vulnerability:** Cross-module table ordering invariant with no guard. Running this script standalone (`python db_migration_snapshots.py`) on a fresh install will crash without explanation.
**Impact:** Fresh-install bug. Also, if `flow_sportsbook.py` is ever refactored to rename `users_table`, this file has no hard dependency declaration.
**Fix:** Either wrap the query in `try/except sqlite3.OperationalError` with a clear log message, or assert the table exists: `con.execute("SELECT 1 FROM users_table LIMIT 1")` first.

### WARNING #3: `datetime.now(timezone.utc).strftime("%Y-%m-%d")` — "today" boundary is UTC, not league timezone
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:54`
**Confidence:** 0.75
**Risk:** Snapshots are keyed on UTC date. If the league runs on ET (most TSL users are US), a snapshot at 7pm ET on day X is actually 00:00 UTC on day X+1 — so it's recorded as the next day. This mis-buckets balance history by one day for any evening activity. Sparklines will show "day X's balance" as the 19:00 ET value of day X-1.
**Vulnerability:** Timezone assumption mismatch. Also: `INSERT OR REPLACE` means the LAST snapshot of the day wins, but the UTC day rolls over at 7pm ET — so the 19:01 ET snapshot replaces the 18:59 ET one and gets tagged as day X+1.
**Impact:** Sparkline history is off-by-one-day for evening activity, specifically around balance inflection points (post-game payouts land in the wrong day).
**Fix:** Use `datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")` or a configurable `LEAGUE_TZ` env var. Document the choice.

### WARNING #4: Embedded Python code string `DISCORD_TASK_TEMPLATE` is dead — CLAUDE.md says file is not loaded as cog
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:119-144`
**Confidence:** 0.85
**Risk:** The `DISCORD_TASK_TEMPLATE` string contains a `commands.Cog` skeleton intended to be copied into a tasks cog. Per CLAUDE.md: "db_migration_snapshots.py — standalone migration utility (synchronous sqlite3.connect); not loaded as a cog." The template is dead documentation that references `setup_snapshots_table()` (synchronous) being called inside a `@tasks.loop` coroutine WITHOUT `asyncio.to_thread()`. A developer who copies this template verbatim will create a blocking I/O bug in an `async` event loop.
**Vulnerability:** Dead template contains a pattern explicitly prohibited by CLAUDE.md ("Must be called via asyncio.to_thread() if ever wired into async context"). Anyone copying it creates a silent blocking-I/O bug.
**Impact:** Latent footgun. A future contributor copies the template and blocks the bot's event loop on every daily snapshot.
**Fix:** Either delete the template string entirely, or rewrite it to wrap the calls in `await asyncio.to_thread(setup_snapshots_table)` and `await asyncio.to_thread(take_daily_snapshot)`.

### OBSERVATION #1: `__main__` block runs `setup_snapshots_table()` unconditionally — no dry-run or confirmation
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:147-154`
**Confidence:** 0.6
**Risk:** Running `python db_migration_snapshots.py` immediately creates the table on the prod DB (since `DB_PATH` is the live `flow_economy.db` by default). No `--dry-run`, no `--confirm`, no interactive prompt. A misclick or accidental CI hook runs the migration on the wrong database.
**Vulnerability:** No rollback/confirmation safety for a DDL operation.
**Impact:** Low — `CREATE TABLE IF NOT EXISTS` is idempotent. But setting a bad example for future migrations.
**Fix:** Add `--dry-run` flag or require `--confirm` to run.

### OBSERVATION #2: No logging module — uses `print()` for status
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:46, 64, 114`
**Confidence:** 0.55
**Risk:** Status messages use `print(f"[ATLAS] ...")`. When this runs inside the bot via `asyncio.to_thread()`, the print goes to stdout, which may or may not be captured by the bot's logging infrastructure. An error state (e.g., the warning above) is invisible in a structured log viewer.
**Vulnerability:** Observability gap — ad-hoc stdout logs vs. structured logging.
**Impact:** Status is visible only in raw stdout; any log-forwarder (systemd, journald, docker-log) needs extra setup to capture.
**Fix:** `import logging; log = logging.getLogger(__name__)` and use `log.info(...)`.

### OBSERVATION #3: `backfill_from_bets()` missing error handling and transaction boundary
**Location:** `C:/Users/natew/Desktop/discord_bot/db_migration_snapshots.py:67-114`
**Confidence:** 0.5
**Risk:** The `with sqlite3.connect(...)` block wraps the entire users loop. A single crash aborts ALL users' backfills. No try/except per-user, no progress reporting. For a backfill on a DB with 100+ users and thousands of bets, a 1-user crash wipes the whole batch.
**Vulnerability:** No per-user fault isolation.
**Impact:** Re-running a failed backfill wastes work.
**Fix:** Per-user commit with try/except, logging skipped users. Or chunk users into batches of 10.

## Cross-cutting Notes

This file has the same truncation-math duplication as `odds_utils.py` — there are now at least two separate implementations of "American odds → profit" math in the codebase, both using floor-truncation. Fixing `odds_utils.payout_calc` to use `round()` (per that file's critical finding) will silently break this module's backfill math. Recommend consolidating: import `profit_calc` from `odds_utils` here. Also, this is the same file that CLAUDE.md specifically calls out ("Must be called via `asyncio.to_thread()` if ever wired into async context") — `flow_sportsbook.py:3111` DOES respect that, but the `DISCORD_TASK_TEMPLATE` on lines 119-144 documents an incorrect non-thread-wrapped pattern. The in-file template contradicts the enforced invariant.

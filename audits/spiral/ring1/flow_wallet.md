# Adversarial Review: flow_wallet.py

**Verdict:** block
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 593
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (4 critical, 7 warnings, 6 observations)

## Summary

This file is the trust root for ATLAS's economy, but the public `debit()` / `credit()` API makes `reference_key` optional with a `None` default — directly contradicting the CLAUDE.md hard rule that "ALL debit calls MUST pass `reference_key`." Combined with a duplicate-suppressing idempotency check that lets the very first un-keyed call succeed without locking, no atomic per-user serialization on the async path, and a sync `update_balance_sync` wrapper whose nested helper executes outside any explicit transaction when `con` is supplied by a caller that forgets `BEGIN IMMEDIATE`, this file currently allows several plausible double-debit / double-credit / silent-loss scenarios under normal Discord retry behavior. Block until the four CRITICALs are addressed.

## Findings

### CRITICAL #1: `reference_key` is optional on `debit()` and `credit()` — direct violation of the documented idempotency contract

**Location:** `flow_wallet.py:215-274` (credit) and `flow_wallet.py:277-346` (debit)
**Confidence:** 0.98
**Risk:** Callers can — and do — invoke `debit()` / `credit()` without passing `reference_key`, in which case `_check_idempotent()` returns `None` immediately and the operation is fully un-deduped. Discord interaction retries, button double-clicks, "this interaction failed → user clicks again" flows, and any `followup.send()` storm can therefore double-debit a user.
**Vulnerability:**
- The signature is `reference_key: Optional[str] = None`. There is no runtime guard that raises if it's missing.
- `_check_idempotent()` (line 174-183) explicitly short-circuits to `return None` when `reference_key` is falsy, so the function silently lets through every un-keyed call without ever inspecting `transactions`.
- The schema declares `reference_key TEXT UNIQUE DEFAULT NULL` (line 474), which in SQLite means `NULL` is allowed multiple times — so the DB does not enforce uniqueness either. Both layers cooperate to make the safety net invisible.
- CLAUDE.md states the rule explicitly: "ALL debit calls MUST pass `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits." This file is the place that rule must be enforced.
**Impact:** Real money corruption: any caller that forgot to pass a key (or that uses the `set_balance` admin path which hardcodes `None`) can produce ledger duplicates that are nearly impossible to recover after-the-fact because there's no surviving handle to dedupe on. Affects every vertical: Sportsbook, Casino, Predictions, Real Sports, Economy.
**Fix:**
1. Make `reference_key` a **required** keyword-only argument: `reference_key: str` (no default). Remove `Optional`.
2. Raise `ValueError("reference_key is required")` at the top of both functions.
3. Tighten the schema: `reference_key TEXT NOT NULL UNIQUE` and add a one-time migration to backfill any historical NULLs with synthetic keys.
4. Add a unit test that asserts calling `debit()` without `reference_key` raises.
5. For the legitimate "no idempotency wanted" case (e.g. some admin tools), force the caller to construct an explicit unique sentinel like `f"NOIDEM_{uuid4()}"` so it's visible in logs.

---

### CRITICAL #2: `set_balance()` hardcodes `reference_key=None`, making admin overrides infinitely retriable

**Location:** `flow_wallet.py:349-395`
**Confidence:** 0.95
**Risk:** The admin override path passes `None` for `reference_key` (lines 372 and 388), so two clicks of an admin "set balance to 5000" button — or two `boss_cog` modal submissions — produce two adjustment transactions instead of one.
**Vulnerability:**
- `set_balance()` is the kind of operation that *most* needs idempotency: it produces a delta-style transaction (`delta = amount - old`) where re-running it after a successful first run computes `delta = 0` and looks fine — but if the caller observes the first run's "old" balance, retries with the same target value, and a *second* admin action has happened in between, the math is silently wrong.
- More concretely, `set_balance(uid, 5000)` followed by `credit(uid, 100)` followed by retry of `set_balance(uid, 5000)` rolls back the credit silently. There is no audit trail signaling that a retry happened — the second txn just shows `delta = -100`.
- The function is called from boss_cog / god_cog admin flows, which ARE subject to Discord interaction retries.
**Impact:** Silent ledger corruption on admin actions, hardest class of bug to trace because there's no error and no duplicate-key constraint to protect us.
**Fix:** Require `reference_key: str` here too. Force boss/god cogs to mint `f"ADMIN_SET_{uid}_{admin_id}_{int(time.time())}"` per click. Add the same NOT NULL UNIQUE constraint.

---

### CRITICAL #3: Async path has no per-user lock — TOCTOU between idempotency check and balance update

**Location:** `flow_wallet.py:215-274` (credit), `flow_wallet.py:277-346` (debit)
**Confidence:** 0.85
**Risk:** When the caller does NOT supply `con`, the function opens its own `aiosqlite` connection, issues `BEGIN IMMEDIATE`, runs the idempotency check, and writes. The `BEGIN IMMEDIATE` does serialize *writers* at the SQLite layer, so the SQL statements within one transaction are safe — but only one transaction at a time will be allowed to write. The problem is what happens to the **second** concurrent caller of an un-keyed (CRITICAL #1) `debit()`: it blocks until the first commits, then proceeds to also debit. There is no "wait, the first caller already paid this" defense at all.
**Vulnerability:**
- `get_user_lock()` exists at lines 30-41 and is even used by `economy_cog.admin_give` (line 82), but `flow_wallet.debit/credit` themselves do NOT acquire it. The lock is a polite convention, not an enforced barrier.
- If two different cogs both call `flow_wallet.debit(uid, 100, ...)` from two different coroutines, one will get the lock-acquired connection first and commit; the second will then run its OWN `BEGIN IMMEDIATE`, observe the new (post-debit) balance, and debit *again*.
- This is fine if `reference_key` is unique-and-passed (the second tx fails the unique constraint or is short-circuited by `_check_idempotent`). But because of CRITICAL #1, that's not guaranteed.
- Even WITH a reference_key, the un-keyed admin paths (CRITICAL #2) are exposed.
**Impact:** Race-induced double-spend. Probability is low under normal load but spikes during burst events (jackpot payouts, mass stipend distribution, casino auto-resolution).
**Fix:** Inside the async `debit()` and `credit()` functions, wrap the `con is None` branch in `async with get_user_lock(discord_id):`. The sync `update_balance_sync` path likewise needs an equivalent global serialization (currently it has none — CRITICAL #4).

---

### CRITICAL #4: `update_balance_sync()` has no idempotency-violation guard, no per-user lock, and trusts callers to pass `BEGIN IMMEDIATE`

**Location:** `flow_wallet.py:527-576`
**Confidence:** 0.92
**Risk:** Three compounding flaws on the sync path used by `flow_sportsbook.py`:
1. **No `_check_idempotent` for caller-supplied `con`**: When `con is not None` (line 569), `_run(con)` executes without the caller having necessarily started a transaction. If the caller just opens `sqlite3.connect()` and passes the raw connection, *autocommit mode* applies — and the UPDATE + INSERT happen in two separate implicit transactions.
2. **The reference_key idempotency check (lines 543-549) lives inside `_run()`** but performs only a `SELECT` — it does not acquire any lock. Two parallel sync threads with the same `reference_key` will both see "no row" and both write.
3. **No per-user lock on the sync path at all.** `get_user_lock` returns an `asyncio.Lock`, which is unusable from sync code, so `flow_sportsbook` settlement cannot serialize even if it wanted to.
**Vulnerability:**
- The negative-balance check at lines 553-556 raises `InsufficientFundsError` AFTER computing `new_balance`, which under autocommit-mode-with-no-BEGIN means the SELECT-then-UPDATE pair isn't atomic. Two concurrent debits of $50 against a $60 balance both see $60, both pass the check, both write — final balance $-40.
- The function relies on the docstring "If con is provided, uses that connection (no commit)" but there is no `c.in_transaction` assertion.
- If `flow_sportsbook` calls `update_balance_sync(...)` from a thread pool worker (which it does for settlement), there is no SQLite cross-process write serialization either, since each thread has its own connection.
**Impact:** Sportsbook settlement is the highest-volume debit/credit path in ATLAS. This is the most likely place for real-money corruption to happen at scale.
**Fix:**
1. Document and enforce that `con` must be in-transaction: assert `c.in_transaction` at the top of `_run()`.
2. When opening own connection (line 572), also acquire a process-wide `threading.Lock()` keyed by `discord_id` (parallel to `get_user_lock`).
3. Add a separate `wait_for_serialization()` helper that sportsbook settlement loops over so we never have two settlement passes interleaved.
4. Make `reference_key` required here too — same fix as CRITICAL #1.

---

### WARNING #1: Idempotency check in `con`-supplied path returns stale `balance_after`, not current balance

**Location:** `flow_wallet.py:236-238, 297-299`
**Confidence:** 0.88
**Risk:** When a caller passes its own `con`, `_check_idempotent` returns `row[0]` which is the `balance_after` from the *original* transaction. But if other transactions have run since then, the actual current balance is different. The caller will get a stale value, then optionally use it for "show user new balance" — leading to a UI that says the user has a stale-but-historical balance.
**Vulnerability:**
- This is an **idempotent operation returning wrong "current" data** — a common source of confusion for callers that interpret the return value as "the user's balance now."
- The async path with `con=None` rolls back (line 257) and returns the same stale value.
- The downstream UI (e.g. `economy_cog`'s "new balance" embed) will display the stale number, which then confuses the user.
**Impact:** User-visible inconsistency, support tickets like "ATLAS told me I had X but Y showed up later." Not a financial loss but a trust erosion.
**Fix:** When the idempotency hit fires, do an extra `SELECT balance FROM users_table WHERE discord_id=?` and return that fresh value instead of the historical `balance_after`. Document that the return value is "current balance after the (no-op) operation."

---

### WARNING #2: `_check_idempotent` does not verify the matched transaction was the same operation

**Location:** `flow_wallet.py:174-183`
**Confidence:** 0.85
**Risk:** The check matches purely on `reference_key`. If a debit and a credit both happen to share the same key (due to a caller bug, test fixture, or a key generator collision), the second one will silently succeed as a no-op. Worse, a debit can be "idempotent-suppressed" by an unrelated credit with the same key.
**Vulnerability:**
- No validation that the matched row's `amount` sign matches the requested operation.
- No validation that the matched row's `discord_id` matches.
- No validation that the matched row's `source` matches.
- Reference keys are constructed by callers like `f"{SUBSYSTEM}_DEBIT_{uid}_{event_id}_{int(time.time())}"`, but a sloppy formatter or a copy-paste from a debit pattern into a credit path could collide.
**Impact:** Silent failure mode: a credit that should have happened doesn't, because a stale debit row matches the same key.
**Fix:** Match on `(reference_key, discord_id, source)` AND assert that `sign(amount)` matches the requested operation. Raise on mismatch — that's a programming bug, not a duplicate.

---

### WARNING #3: Bare `except Exception: pass` in DDL migration suppresses real errors

**Location:** `flow_wallet.py:495-499`
**Confidence:** 0.95
**Risk:** The "ALTER TABLE ADD COLUMN" loop catches *every* exception and silently swallows it. The intent is "column already exists," but this also swallows database lock errors, schema corruption, malformed SQL (if `col` is ever changed to a value with bad syntax), permission errors, etc.
**Vulnerability:**
- Per CLAUDE.md: "Silent `except Exception: pass` in admin-facing views is prohibited" — and while this is a setup function, it's a *startup-critical* path that determines whether the audit trail works.
- If the migration silently fails, all subsequent `_insert_txn` calls write into `(subsystem, subsystem_id)` columns that don't exist, causing every transaction insert to throw — and *that* error will probably be caught somewhere downstream too, hiding the root cause.
**Impact:** Database setup failures masked at startup. Hours of debugging when the real cause is a single permission denied during initial migration.
**Fix:**
```python
for col in ("subsystem TEXT DEFAULT NULL", "subsystem_id TEXT DEFAULT NULL"):
    try:
        await db.execute(f"ALTER TABLE transactions ADD COLUMN {col}")
    except aiosqlite.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise
```

---

### WARNING #4: `_ensure_theme_column` is sync and is called from request handlers

**Location:** `flow_wallet.py:73-84` and `87-95` (`get_theme`)
**Confidence:** 0.85
**Risk:** `get_theme()` and `set_theme()` are sync functions that open a `sqlite3.connect()` synchronously and read/write the DB. They are called by Discord UI views (theme picker) on the event loop thread.
**Vulnerability:**
- Blocking sqlite3 I/O on the asyncio event loop is exactly the pattern flagged by `_atlas_focus.md`: "Blocking calls inside `async` functions (sqlite3, requests, time.sleep). All blocking I/O must go through `asyncio.to_thread()`."
- `_ensure_theme_column` runs *every time* `get_theme` is called (because `_theme_col_checked` is in-process, but only "after first call" — first call always blocks for ALTER TABLE) and could deadlock if a long-running async transaction holds the DB lock.
- The lru_cache on `get_theme_for_render` (line 120) helps but only after first call per user; the cold-cache path still blocks.
**Impact:** Event-loop stalls of tens to hundreds of ms during render bursts. Worst case: ALTER TABLE during a heavy write window blocks the entire bot for the SQLite timeout (10s).
**Fix:** Make these `async` and use `aiosqlite`. Move all schema migration into `setup_wallet_db()` so first-call latency goes away. Keep `get_theme_for_render` sync only as a last-resort cache hit.

---

### WARNING #5: `setup_wallet_db()` does not migrate `reference_key` to NOT NULL or enforce a uniqueness invariant on legacy NULLs

**Location:** `flow_wallet.py:460-507`
**Confidence:** 0.80
**Risk:** The `transactions` table is created with `reference_key TEXT UNIQUE DEFAULT NULL`. SQLite allows multiple NULLs in a UNIQUE column, so any historical rows inserted without a key (from `set_balance`, from the `_insert_txn` ADMIN path, etc.) cannot be deduped retroactively, and they pollute future idempotency checks (matching any NULL would be a bug, but the code uses the `_check_idempotent` short-circuit which prevents that — at the cost of CRITICAL #1).
**Vulnerability:**
- The lack of NOT NULL means the schema cannot help enforce CRITICAL #1 / CRITICAL #2. If those fixes are applied, this schema must be migrated too.
- `idx_tx_ref` on `(reference_key)` will be huge if many rows are NULL (NULL is indexed as a sentinel in SQLite), and the index does no work for those rows.
**Impact:** Schema cannot help catch the bugs above. Index bloat.
**Fix:** Add a one-time migration that backfills NULL `reference_key` with `f"LEGACY_{txn_id}"`, then `CREATE INDEX ... WHERE reference_key IS NOT NULL` (or migrate to NOT NULL after backfill).

---

### WARNING #6: `backfill_subsystem_tags()` has no idempotency / partial-failure recovery, and no audit log

**Location:** `flow_wallet.py:579-593`
**Confidence:** 0.75
**Risk:** This function is documented as "one-time migration." It is not protected against:
- Being called twice (it's idempotent because it filters `subsystem IS NULL`, so OK there)
- Being interrupted partway: if it crashes after updating TSL_BET rows but before CASINO, the partial result is committed only at the very end (line 592 — single commit), so an interrupted run leaves *zero* rows updated. That's safer than half-updated, but the operator gets no signal as to where it stopped.
- Incorrectly mapping rows whose `source` is `TSL_BET` but whose actual subsystem is something else (e.g. a TSL BET row that was actually a casino-cross-promo).
**Vulnerability:**
- No logging — caller has no visibility into how many of each source were updated.
- No transaction wrapping — if SQLite crashes mid-loop after one source's UPDATE has applied to its WAL (and another hasn't), the WAL state vs. user expectation diverges silently.
- Function is in the same module as all the other public APIs but is named like a private utility.
**Impact:** A migration tool that fails silently is a recipe for "audit trail says X, reality is Y" support requests.
**Fix:** Wrap each source UPDATE in its own transaction, log the rowcount per source, and return a dict instead of a total. Add a `dry_run=True` mode.

---

### WARNING #7: `get_user_lock()` cleanup is racy — locks may be evicted while a coroutine is about to acquire

**Location:** `flow_wallet.py:34-48`
**Confidence:** 0.70
**Risk:** `get_user_lock` checks `if uid not in _user_locks`, then conditionally calls `_cleanup_idle_locks()`, then creates a new lock. But `_cleanup_idle_locks()` iterates `_user_locks.items()` and deletes any lock that is `not lock.locked()`. There's a sequence:
1. Coroutine A calls `get_user_lock(123)` → cleanup runs, deletes lock for uid 456 (which is not currently held)
2. Coroutine B was just about to `async with get_user_lock(456):`. It calls `get_user_lock(456)` → gets a *new* lock (different identity from the one A's contemporary held)
3. A third coroutine C — running concurrently — already had the *original* lock for 456 and is mid-write
4. B's new lock is uncontended, so B writes simultaneously with C
**Vulnerability:**
- The "weak refs" comment on line 28 implies `weakref` should be used, but the code uses a plain `dict[int, asyncio.Lock]`. The comment is misleading — there is no actual weak ref protection.
- `_cleanup_idle_locks` is not actually safe for in-flight callers because there's no atomicity between "I called get_user_lock and got a Lock object" and "I am about to acquire it."
**Impact:** Under high concurrency for the same user (rapid-fire button clicks), lock identity can flip between cleanup and acquisition, defeating the serialization. Probability is low because debit/credit don't use the lock at all (CRITICAL #3) — but if/when they do, this becomes a real concurrency hazard.
**Fix:** Either (a) use `weakref.WeakValueDictionary` as the comment suggests, with explicit reference holding by callers, or (b) never delete locks for users who currently hold the lock OR are queued on it (but `asyncio.Lock` doesn't expose queue length cleanly) — the simplest fix is to bound the dict size and only cleanup when length > threshold AND only for entries with `not locked()`. The code does (b)-ish but the cleanup loop runs *before* the new lock is created, opening the race window.

---

### OBSERVATION #1: `get_balance()` commits an empty transaction when the user already exists

**Location:** `flow_wallet.py:204-212`
**Confidence:** 0.90
**Risk:** When called without `con`, this function opens a connection, calls `_ensure_user`, and unconditionally commits. If the user already exists, `_ensure_user` does no INSERT, so the commit is over an empty transaction. This is harmless but wasteful — and worse, it means every read of a balance triggers a write-lock acquisition.
**Vulnerability:** Performance: under read-heavy load (leaderboards, dashboard renders), this cascades into write-lock contention against actual debits/credits.
**Impact:** Slow reads under concurrency, especially during settlement.
**Fix:** Only call `db.commit()` if `_ensure_user` actually inserted. Easiest: have `_ensure_user` return a tuple `(balance, was_created)`.

---

### OBSERVATION #2: No upper bound on `amount` — single-call balance overflow possible (Python int is unbounded but SQLite INTEGER is 64-bit)

**Location:** `flow_wallet.py:215-346`
**Confidence:** 0.65
**Risk:** Both `credit()` and `debit()` validate `amount > 0` but not `amount < some_max`. A bug in a caller (or a malicious admin command) that passes `amount = 10**20` will:
- Succeed in Python arithmetic
- Possibly overflow when SQLite tries to store the resulting `balance` (SQLite INTEGER max = 9.2 × 10^18)
- Possibly silently truncate or wrap on `balance_after` insert
**Vulnerability:** No defense in depth. The economy presumably has reasonable bet caps but those are enforced upstream, not here.
**Impact:** Edge case but catastrophic if hit. Amount bounds at the trust root help every caller.
**Fix:** Add `if amount > 10**12: raise ValueError("amount exceeds wallet maximum")`. Pick a number above any plausible legitimate transaction.

---

### OBSERVATION #3: `get_total_supply` returns `row[0]` but `row` could theoretically be `None` if SQLite returns nothing

**Location:** `flow_wallet.py:450-457`
**Confidence:** 0.40
**Risk:** `SELECT COALESCE(SUM(balance), 0) FROM users_table` is guaranteed to return one row, so `row` should never be None — but the code doesn't defend. If the schema migration hasn't run yet (e.g. fresh DB before `setup_wallet_db`), the `users_table` may not exist and `aiosqlite.execute` will raise `OperationalError`, which is unhandled.
**Vulnerability:** Cold-start path can crash.
**Impact:** Bot startup race condition where a dashboard loads before `setup_wallet_db()` finishes.
**Fix:** Wrap in try/except and return 0 with a log, OR ensure callers wait on a startup readiness future.

---

### OBSERVATION #4: `get_transactions` does not validate `limit` — caller can ask for `limit=10**9`

**Location:** `flow_wallet.py:410-434`
**Confidence:** 0.55
**Risk:** No upper bound on `limit`. A caller passing `limit=1_000_000` will materialize a million-row result set in memory.
**Vulnerability:** Memory exhaustion on the bot process. Most callers probably pass small limits, but the public API doesn't enforce.
**Impact:** OOM if a misconfigured admin command or test fixture passes a huge limit.
**Fix:** `limit = min(limit, 1000)` at the top.

---

### OBSERVATION #5: `_now()` returns a string, not a UNIX timestamp — sortable but slow to compare

**Location:** `flow_wallet.py:139-140` and all `created_at` insertions
**Confidence:** 0.60
**Risk:** ISO-8601 strings sort lexicographically as long as they're always UTC with offset, which they are. But:
- The schema has no `created_at` index for time-range queries.
- Indexed queries with WHERE on `created_at` will use a string comparison instead of an integer comparison — slower.
- `season_start_balance` is stored on the user row but there's no `season_start_at` to anchor "this season started when" — making historical reconstruction impossible.
**Vulnerability:** Future analytics queries will be slow or impossible.
**Impact:** Performance and feature limitations downstream.
**Fix:** Add a `created_at_unix INTEGER` column (epoch seconds), index it, and write both columns on insert. Migrate existing rows lazily.

---

### OBSERVATION #6: Module imports `wager_registry` *inside* `setup_wallet_db()` (line 505) instead of at top of file

**Location:** `flow_wallet.py:505-506`
**Confidence:** 0.50
**Risk:** Lazy import inside a function is usually a circular-dependency workaround. If `wager_registry` ever changes its dependency graph or if `setup_wallet_db()` is called from an unusual context (test, REPL), the import may fail in a confusing way.
**Vulnerability:** The comment says "(GAP 5)" — implies it's a known wart. There's no docstring explaining *why* it's local.
**Impact:** Maintenance burden, harder to test, surprising for new readers.
**Fix:** Move the import to the top with a try/except guard, OR document the circular dependency in a comment.

---

## Cross-cutting Notes

This file is the Flow / Economy trust root for the entire ATLAS bot. The single most important fix is making `reference_key` a required argument on `debit()`, `credit()`, and `set_balance()` — once that ships, the schema constraint (NOT NULL UNIQUE) and the per-user lock can both lean on the same invariant. Until then, every caller in `flow_sportsbook.py`, `economy_cog.py`, `casino/`, `polymarket_cog.py`, `real_sportsbook_cog.py`, and `flow_store.py` is one missed keyword argument away from a double-spend bug, and the trust root cannot detect or prevent it.

Once `reference_key` is required, the next round of audit work in Ring 1 Batch B should:
1. Audit every call site that imports `flow_wallet` and verify each `debit/credit` passes `reference_key=...` (per CLAUDE.md, this is already supposed to be true — but the type system doesn't enforce it).
2. Check whether `flow_sportsbook.py` settlement uses `update_balance_sync` with a caller-supplied `con` that is in `BEGIN IMMEDIATE` (the assertion suggested in CRITICAL #4 will catch any that aren't).
3. Verify that the per-user `asyncio.Lock` and the proposed sync `threading.Lock` are not used cross-thread (if a sync wrapper is called from `asyncio.to_thread()`, mixing the two lock types becomes confusing).

The async vs. sync split (`debit/credit` async, `update_balance_sync` sync) is itself a smell — either flow_sportsbook should be migrated to async, or the wallet should expose a single sync API and let callers use `asyncio.to_thread()` themselves. Maintaining two parallel implementations of the same financial primitive is exactly the kind of split-brain that produces silent ledger drift.

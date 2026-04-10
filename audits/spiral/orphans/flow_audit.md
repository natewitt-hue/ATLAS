# Adversarial Review: flow_audit.py

**Verdict:** needs-attention
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 676
**Reviewer:** Claude (delegated subagent)
**Total findings:** 12 (2 critical, 5 warnings, 5 observations)

> ORPHAN STATUS: LIVE
> This file is not imported through bot.py's direct dependency chain but IS imported by active code: `economy_cog.py`. Argus's static scan missed it. Review as active production code.

## Summary

Read-only audit engine for `flow_economy.db` with 10 checks. The file is genuinely read-only (all `SELECT`s, no mutations) and most checks are well-formed. The two real problems are both in `check_balance_drift` and `check_transaction_continuity`: they compare integer balances with `!=`, which is correct for integers but corrupts silently under float drift (and CLAUDE.md specifically calls out "float vs int balance corruption in flow_economy.db"). There is also a schema drift risk — multiple checks query tables like `bets_table`, `users_table`, `parlay_legs`, `transactions`, `wagers` without any `try/except sqlite3.OperationalError` guard, so a missing-column or missing-table migration regression takes down the whole audit (the `check_stuck_predictions` and `check_jackpot_sanity` checks DO wrap with `sqlite_master` lookups — others don't).

## Findings

### CRITICAL #1: `check_balance_drift` uses `!=` comparison on balances that may be floats — silently false-positives or false-negatives

**Location:** `flow_audit.py:247-276`
**Confidence:** 0.9
**Risk:** Line 266 computes `drift = current - last_txn` and line 266 checks `if drift != 0:`. If `users_table.balance` or `transactions.balance_after` is stored as REAL (even transiently via a bug elsewhere), floating-point arithmetic produces tiny non-zero drifts like `1e-15` on every user, flooding the audit with false CRITICALs. Conversely, if one value is `1000` (int) and the other is `1000.0000000001` (float from a poorly-implemented payout), the audit catches it — but displays "drift: +0" due to `:+,` format, confusing the admin. CLAUDE.md explicitly flags "Float vs int balance corruption in flow_economy.db" as an ATLAS attack surface.
**Vulnerability:** No tolerance window, no type coercion, no CAST in the SQL.
**Impact:** Either a false-positive storm that trains admins to ignore balance drift alerts (silencing real corruption), or a silent-false-negative where tiny drifts below display resolution hide a real leak.
**Fix:** `CAST` both values in SQL: `CAST(u.balance AS INTEGER) as balance, CAST((SELECT balance_after FROM ...) AS INTEGER) as last_txn_balance`. Then `if abs(drift) > 0:` with the integer guarantee. For detecting float corruption *itself*, add a separate check that flags any row where `typeof(balance) != 'integer'`.

### CRITICAL #2: `check_transaction_continuity` self-joins on `MAX(txn_id) < t1.txn_id` — O(N²) on the whole transactions table with no index hint

**Location:** `flow_audit.py:563-608`
**Confidence:** 0.85
**Risk:** The subquery `SELECT MAX(txn_id) FROM transactions WHERE discord_id = t1.discord_id AND txn_id < t1.txn_id` runs *once per row in the outer query*, and the outer query scans every transaction. With an index on `(discord_id, txn_id)` this is O(N log N); without it, O(N²). There's no index definition visible in this file. The `LIMIT 50` at the end only caps the result set, not the scan — SQLite still evaluates every row before applying LIMIT. On a production DB with millions of transactions (plausible after 95+ seasons), this query can lock the database for 10+ seconds. Since the audit is triggered by `/boss flow audit` and by a daily `@tasks.loop` in `economy_cog`, a 10-second SQLite lock during the daily audit blocks every other wallet operation (casino spins, bet placements, parlay settlements) for the duration. Discord user-facing commands time out at 3 seconds; modal submissions time out at 15.
**Vulnerability:** Unindexed self-join that grows with data; no `EXPLAIN QUERY PLAN` check; no batching.
**Impact:** Daily audit run at 3am is fine. Ad-hoc `/boss flow audit` during peak hours blocks wallet operations for 10+ seconds, causing user-facing timeouts and potentially corrupting bet/wager idempotency retries (Discord double-clicks during the lock window). This ties directly back to the CLAUDE.md Flow Economy rule about idempotency via `reference_key`.
**Fix:** (a) Require an index `CREATE INDEX IF NOT EXISTS idx_transactions_user_txn ON transactions(discord_id, txn_id DESC)` and ship it in a migration. (b) Rewrite as a window function: `SELECT ..., LAG(balance_after) OVER (PARTITION BY discord_id ORDER BY txn_id) as prev_after FROM transactions`. (c) Limit the search space with `WHERE t1.txn_id > (SELECT MAX(txn_id) - 10000 FROM transactions)` for ad-hoc runs.

### WARNING #1: Check functions open a new `aiosqlite.connect(self.db_path)` on every call — 10 connections per audit

**Location:** `flow_audit.py:156-608`
**Confidence:** 0.9
**Risk:** Every one of the 10 check functions does `async with aiosqlite.connect(self.db_path) as db:` at its start, so a full audit opens, uses, and closes 10 separate SQLite connections sequentially. Each connection pays the WAL setup cost and any `PRAGMA`s. Worse, the connections are not shared — if SQLite has a read lock contention (e.g. during a write), one check can fail while another succeeds, producing inconsistent findings from different snapshots of the DB.
**Vulnerability:** No shared connection or transaction across checks. `run_all` sees 10 different DB snapshots.
**Impact:** Subtle inconsistency — a transaction settled between check #1 and check #5 could appear as "orphaned" in one and "settled" in the other. Also wasted connection overhead.
**Fix:** Open one connection at the top of `run_all`, begin a `BEGIN DEFERRED` transaction for a consistent read snapshot, and pass the connection to each check function as a parameter. Commit/rollback at the end.

### WARNING #2: `_age_days` runs a SQL query just to compute days — wasteful and brittle

**Location:** `flow_audit.py:612-619`
**Confidence:** 0.85
**Risk:** `_age_days` executes `SELECT CAST(julianday('now') - julianday(?) AS INTEGER)` for every orphaned bet found. For 50 orphaned bets that's 50 extra round trips to the DB. And it assumes the input `created_at` is in a format SQLite `julianday` understands — if the format drifts (e.g. Python `datetime.isoformat()` with microseconds), `julianday` returns NULL, and the function returns `0`, making every orphaned bet appear to be "0 days old" and downgrading them from HIGH to MEDIUM severity.
**Vulnerability:** SQL-based date math with no format validation.
**Impact:** Silent severity downgrade on orphaned bets when timestamp format drifts. An orphaned 30-day-old bet could be reported as 0 days old.
**Fix:** Compute in Python: `from datetime import datetime, timezone; age = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).days`. Handle parse errors explicitly.

### WARNING #3: `check_orphaned_bets` uses `datetime('now', '-3 days')` — ambiguous timezone

**Location:** `flow_audit.py:165-169, 184-191`
**Confidence:** 0.8
**Risk:** SQLite's `datetime('now', '-3 days')` returns UTC time. If `bets_table.created_at` is stored in *local* time (or vice versa), the 3-day threshold is off by the local UTC offset — in US Eastern (-5), a bet made "3 days ago local" is not yet flagged until 5 hours later. There's no visible documentation of which timezone `created_at` uses. The same ambiguity exists at lines 335 (`'-1 hour'`) and 395 (`'-30 days'`) for other checks.
**Vulnerability:** Timezone-mixing trap. CLAUDE.md flags identity/username resolution hazards, but not timezone hazards specifically — still, the underlying issue is the same category of "assumption mismatch across subsystems".
**Impact:** Orphaned-bet checks fire 5 hours later than intended on Eastern-deployed bots, or stay silent entirely if `created_at` is in local time far from UTC.
**Fix:** Normalize all timestamps to UTC at write time and document it in `wager_registry.py`'s `_now()`. Verify `bets_table.created_at` is also UTC ISO format.

### WARNING #4: `check_missing_wager_entries` at severity LOW — but it directly detects ledger corruption

**Location:** `flow_audit.py:495-529`
**Confidence:** 0.8
**Risk:** A transaction tagged with `subsystem IN ('TSL_BET', 'PARLAY', 'CASINO', 'PREDICTION')` and `amount < 0` represents money that was *debited for a wager*. If the corresponding `wagers` row is missing, the wager registry has lost track of user money. The severity is set to LOW with the suggested action "Backfill wager registry or investigate gap" — but a missing registry entry means the user's wager has *no settlement path*. It will never pay out even on a winning bet. That is a financial corruption scenario, not a low-severity smell.
**Vulnerability:** Severity underweighted relative to the financial impact.
**Impact:** Users with winning bets whose wager registry entry was never created get silently robbed. The CRITICAL category includes "Error bets" but not this class of corruption.
**Fix:** Bump severity to HIGH (or CRITICAL if `amount` is large). Add a secondary check that matches transactions to settlements: if a debit transaction has no `credit` counterpart within 30 days, flag as CRITICAL.

### WARNING #5: `check_error_bets` has no severity ladder by age

**Location:** `flow_audit.py:208-243`
**Confidence:** 0.6
**Risk:** Every bet/parlay in Error state is reported at severity CRITICAL regardless of age. In practice, an Error state is often transient (a network blip during settlement that gets resolved minutes later). If the daily audit runs while a bet is briefly in Error, it fires a CRITICAL. Admins get alert fatigue and start ignoring the alerts, and the real CRITICAL (a bet stuck in Error for 7+ days with the user's money locked) gets lost in the noise.
**Vulnerability:** No distinction between transient and persistent Error states.
**Impact:** False-positive storms from transient Error states → alert fatigue → silent failure of the monitoring system itself.
**Fix:** Join on bet creation/update time and ladder severity by age: `created_at > datetime('now', '-1 hour')` → skip; `<1 day` → MEDIUM; `<7 days` → HIGH; `>=7 days` → CRITICAL. Matches the `check_orphaned_bets` severity ladder.

### OBSERVATION #1: `to_embed_dict` truncates finding lists to 5 per severity with no total count anywhere in the embed

**Location:** `flow_audit.py:79-100`
**Confidence:** 0.9
**Risk:** The embed shows 5 findings per severity and adds `"... and {N} more"` — but the embed has no "grand total" line and no severity counts in the embed title. An admin glancing at the embed sees 5 CRITICAL items and thinks that's the whole story, missing the `"... and 47 more"` buried 3 sections down. The embed color is correct (ERROR for CRITICAL), but the visual hierarchy doesn't broadcast total count.
**Vulnerability:** Display truncation with no summary preserved.
**Impact:** Admins under-react to large finding counts; the summary_text is good but it's only one line.
**Fix:** Add a `"title": f"Flow Economy Audit Report — {len(report.findings)} findings"` or put counts in the description header.

### OBSERVATION #2: `check_stuck_predictions` queries `prediction_contracts` and `prediction_markets` without checking both exist

**Location:** `flow_audit.py:351-411`
**Confidence:** 0.85
**Risk:** Line 358 checks if `prediction_contracts` table exists. If it exists but `prediction_markets` does NOT (schema drift during a migration), the JOIN at line 365 fails with `OperationalError: no such table: prediction_markets`, the audit's outer try/except catches it, and the whole check reports as a "check raised exception" HIGH finding. The cleanup works but leaks implementation details ("no such table") into the audit report. Also at line 361: returning `results` (empty list) when `prediction_contracts` is missing silently hides any stuck prediction findings — if that table is ever renamed, the audit will report "all clear" for stuck predictions forever without anyone noticing.
**Vulnerability:** Partial schema check; silent skip on missing table.
**Impact:** Low. Audit skipping itself is better than crashing, but the skip is invisible.
**Fix:** Check BOTH tables exist; log a warning at INFO level when the check is skipped due to missing tables so operators know the check isn't running.

### OBSERVATION #3: `check_balance_drift` falls through `continue` when `last_txn is None` — new users are invisible to the audit

**Location:** `flow_audit.py:260-264`
**Confidence:** 0.7
**Risk:** A new user with no transactions yet is skipped by `continue` at line 264. If that user's balance is non-zero *without* a transaction record (which would itself be corruption — balance set outside the wallet layer, as the CRITICAL description at line 272 warns about), the audit misses it. The check is designed to catch exactly this bug class but silently ignores the subset of users where the evidence is most damning.
**Vulnerability:** Skip logic hides the exact corruption the check is designed to catch.
**Impact:** A `set_balance` override bug on new users goes undetected.
**Fix:** Change the `continue` to: `if last_txn is None and current != 0: flag as CRITICAL "new user with non-zero balance and no transaction history"`.

### OBSERVATION #4: `check_parlay_consistency` is missing a check for "all legs lost but parlay Won"

**Location:** `flow_audit.py:415-471`
**Confidence:** 0.6
**Risk:** The check catches (a) settled parlays with pending legs and (b) pending parlays with all legs resolved. It does NOT catch the cross-contradiction: a parlay marked Won but one of its legs is Lost (which should make the parlay Lost). This is exactly the class of settlement bug the check is designed to catch but the invariant is only partial.
**Vulnerability:** Incomplete invariant coverage.
**Impact:** Parlay settlement corruption is not fully detected. A bug in settlement logic could silently mark a losing parlay as Won and pay out the user.
**Fix:** Add a third query: `SELECT pt.parlay_id FROM parlays_table pt JOIN parlay_legs pl ON pl.parlay_id = pt.parlay_id WHERE pt.status = 'Won' AND pl.status = 'Lost' GROUP BY pt.parlay_id`.

### OBSERVATION #5: `main()` uses `asyncio.run()` but the file is also imported — cleanup risk

**Location:** `flow_audit.py:662-676`
**Confidence:** 0.5
**Risk:** `main()` at the bottom runs `asyncio.run(run_audit_standalone(...))`. If this module is ever imported into a context where an event loop is already running (which is how `economy_cog` imports it), and someone accidentally calls `main()` from that context (e.g. a new `/boss flow audit-cli` that shells through), `asyncio.run` raises `RuntimeError: asyncio.run() cannot be called from a running event loop`. The `if __name__ == "__main__":` guard prevents this today but is brittle.
**Vulnerability:** Module dual-use pattern with no separation of standalone vs library surface.
**Impact:** Theoretical. Today `main()` is only called from the `if __name__ == "__main__":` block.
**Fix:** Split into `flow_audit.py` (library) and `flow_audit_cli.py` (standalone entry point).

## Cross-cutting Notes

The strongest pattern across this file is **inconsistent defensive coding**: two checks wrap in `sqlite_master` lookups (`check_stuck_predictions`, `check_jackpot_sanity`), eight do not. The CLAUDE.md rule about `sportsbook_cards._get_season_start_balance()` wrapping in `try/except sqlite3.OperationalError` applies here too — `check_balance_drift`, `check_transaction_continuity`, `check_orphaned_bets`, and `check_error_bets` all make assumptions about the schema of `users_table`, `transactions`, `bets_table`, and `parlays_table` that could be violated by an incomplete migration. Consider a schema-probe helper that runs once at `FlowAuditor.__init__` time and skips checks whose required tables/columns are missing, rather than relying on per-check try/except + outer exception handling.

Also worth flagging: the audit is explicitly read-only per the docstring, and a spot-check of all 10 queries confirms that — this is a genuine audit module, not a mutating one. That's a good baseline; the findings above are refinements, not repudiations.

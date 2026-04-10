# Adversarial Review: wager_registry.py

**Verdict:** block
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 614
**Reviewer:** Claude (delegated subagent)
**Total findings:** 19 (4 critical, 9 warnings, 6 observations)

## Summary

The wager registry is the audit trail of record for every wager across Sportsbook, Casino, PvP, Predictions, and Jackpots — but its `backfill_wagers()` early-exit guard is structurally wrong, creating a permanent "history is silently incomplete" failure whenever any live wager is written before backfill runs (a guaranteed condition because cogs are loaded before backfill in `bot.py`). The `update_wager_status` SELECT-then-UPDATE pattern is a TOCTOU race that lets two concurrent settlements both pass the transition check, the public `register_wager()` API silently no-ops on retry without surfacing the existing row's stale values, and several backfill paths will infinite-loop on the next startup if a single source row has unexpected NULLs (no per-row try/except, atomic transaction guarantees the next run repeats the same crash). Block until the early-exit guard, the TOCTOU update, and the per-row error isolation are fixed.

## Findings

### CRITICAL #1: `backfill_wagers()` early-exit guard permanently silences history backfill the moment any live wager exists

**Location:** `wager_registry.py:357-369` (top of `backfill_wagers`)
**Confidence:** 0.95
**Risk:** The function checks `SELECT COUNT(*) FROM wagers` and returns 0 if the table is non-empty. But cogs are loaded BEFORE this function runs (`bot.py:261-289` — extensions load on lines 261-266, `setup_wallet_db` + `backfill_wagers` only on lines 269-289). Any user who places a TSL bet, settles a casino hand, or buys a prediction contract during the cog-load → backfill window writes to `wagers` first. On the very next startup, `COUNT(*) > 0` is true and the entire historical backfill is permanently skipped — bets/parlays/props/casino sessions/predictions from before the registry existed are never imported.
**Vulnerability:**
- The early-exit checks the global table count, not a per-source "have I imported this set yet" marker.
- The comment on line 365 even acknowledges "INSERT OR IGNORE is the real guard," which is correct — the early-exit is purely an optimization, but it silently breaks the safety claim.
- There is no log message explaining why the function returned 0. Operators see "no historical wagers to backfill" on first startup and assume success.
- Concurrent risk amplification: `bot.py:241-266` loads `flow_sportsbook`, `casino.casino`, `polymarket_cog` BEFORE `flow_wallet.setup_wallet_db()` (line 271). Any of those cogs publishing a bet during their `setup` or via a background task will pre-poison the table.
**Impact:** The unified history view, P&L summaries, and all "lifetime stats" surfaces will silently miss large portions of pre-migration data forever, with no warning. Disputes that depend on the registry will be unanswerable for legacy bets. The audit trail this file is supposed to BE has a structural blind spot.
**Fix:**
1. Remove the early-exit entirely. `INSERT OR IGNORE` is already idempotent — let it run every startup. Per-table existence checks (e.g., `SELECT 1 FROM wagers WHERE subsystem='TSL_BET' LIMIT 1`) are safer if you really want to skip work.
2. Better: introduce a `backfill_runs (subsystem TEXT PRIMARY KEY, completed_at TEXT)` table and only skip a section after it has actually completed end-to-end.
3. Add an explicit `INFO` log on every section: `"backfill_wagers: TSL_BET imported X new rows (Y existing skipped)"`.

---

### CRITICAL #2: TOCTOU race in `update_wager_status` / `update_wager_status_sync` allows two concurrent settlements past the transition guard

**Location:** `wager_registry.py:153-190` (async) and `wager_registry.py:266-302` (sync)
**Confidence:** 0.92
**Risk:** The function reads the current status with one statement and updates with a second statement, with no row lock or `BEGIN IMMEDIATE` between them. Two concurrent callers can both read `status='open'`, both pass the `VALID_TRANSITIONS` check, and both UPDATE — the second silently overwrites the first. If caller A is settling as "won" and caller B is voiding (admin correction), the result depends on race ordering and may not match either caller's intent.
**Vulnerability:**
- No `BEGIN IMMEDIATE` opens the implicit transaction in either the async or sync path.
- The async path runs in aiosqlite, which serializes commands per connection but a NEW connection is opened each call (`aiosqlite.connect(DB_PATH)` line 188), so two concurrent calls hold two different connections that race at the SQLite engine level.
- The sync path explicitly opens a fresh connection per call (`_db_con_sync()` line 300), guaranteeing no shared lock.
- The status check is the ONLY guard on a "settled wager cannot be re-settled" invariant — this is the entire point of `VALID_TRANSITIONS`. Bypassing it means a "won" wager could be re-marked "lost" by a stale settlement path.
- Settlement is exactly the path most likely to retry/race: `flow_sportsbook.py:3614` (autograde), `sportsbook_core.py:216` (settle_event with bus + 10-min poller fallback), `casino_db.py:1005` (per-session settle).
**Impact:** A "won" wager could be silently flipped to "lost" or "voided," producing wrong P&L in `get_wager_summary`, wrong record in user-facing surfaces, and (worst case) hiding a real settlement bug under the rug because the registry tells two different stories on different reads.
**Fix:**
1. Open the connection with `aiosqlite.connect(DB_PATH, isolation_level=None)` and execute `BEGIN IMMEDIATE` before the SELECT to take a write lock.
2. Or collapse to a single statement: `UPDATE wagers SET status=?, result_amount=COALESCE(?, result_amount), settled_at=? WHERE subsystem=? AND subsystem_id=? AND status IN (allowed_predecessors)` — then check `cursor.rowcount` and raise `InvalidTransitionError` if 0.
3. The single-statement approach is preferable: it eliminates the race and reduces the surface area of the function.

---

### CRITICAL #3: Per-row exceptions in any `backfill_*` step abort the entire transaction and the next startup will infinite-loop on the same poison row

**Location:** `wager_registry.py:357-517` (`backfill_wagers`), `wager_registry.py:524-584` (`backfill_pvp_wagers`), `wager_registry.py:587-614` (`backfill_jackpot_wagers`)
**Confidence:** 0.88
**Risk:** Every backfill function does all of its work inside a single `aiosqlite.connect(...)` context with one `commit()` at the very end. Any per-row exception (NULL `tier` in `casino_jackpot_log` causing `tier.upper()` AttributeError on line 608, non-numeric `user_id` causing `int(user_id)` ValueError on line 510, NULL `wager_amount` in `prop_wagers` violating the wagers NOT NULL constraint, malformed `created_at` timestamp string, etc.) raises out of the loop, the transaction rolls back, and the function re-raises. On the next startup the same poison row is hit again → another full rollback → another `setup_hook` failure. The entire `Flow wallet system initialized` block (`bot.py:268-292`) is wrapped in `except Exception as e: print(...)`, so the bot will appear to start "successfully" but the registry will be empty AND every subsequent startup will hit the same crash, with no per-row context in the log to identify which row is bad.
**Vulnerability:**
- Lines 374-403, 406-435, 438-467, 470-485, 488-514, 542-552, 568-581, 600-611 all iterate without per-row try/except.
- `bot.py:291-292` swallows the exception with only `print(f"... failed: {e}")` — the row that caused the failure is never identified.
- Schema declares `wagers.discord_id INTEGER NOT NULL` and `wagers.wager_amount INTEGER NOT NULL` (lines 59-60), but `prop_wagers` (`flow_sportsbook.py:278-288`) has neither column declared NOT NULL — a single legacy NULL row poisons the backfill forever.
- `casino_jackpot_log.tier` is declared `TEXT NOT NULL` but if any pre-constraint row exists (legacy / hand-edited), `tier.upper()` is unguarded.
- `prediction_contracts.user_id` is `TEXT NOT NULL` but `int(user_id)` (line 510) is unguarded against the `__probe__` rows that `_migrate_contracts_sold_status` (`polymarket_cog.py:579`) writes during the v2 migration — if that probe ever fails to clean up, the registry can never backfill.
**Impact:** Audit trail can be permanently empty after a single bad row, and recovery requires manual DB inspection to find the offending row because the error message is just `"Flow wallet setup failed: ..."` with no row identifier.
**Fix:**
1. Wrap each row's INSERT in `try / except Exception as e: log.exception("backfill_wagers: skipping bad %s row %s: %s", subsystem, row_id, e); continue`.
2. Coerce nullable fields with explicit defaults: `tier = (tier or "UNKNOWN").upper()`, `try: uid = int(user_id) except ValueError: continue`.
3. Add `defensive` NOT NULL guards: `if discord_id is None or wager_amount is None: log.warning("skip"); continue`.
4. Log the count of skipped rows alongside the imported count so operators see the data quality issue without needing to dig.

---

### CRITICAL #4: `register_wager` / `register_wager_sync` silently return the existing row without updating it on retry, hiding caller-visible bugs

**Location:** `wager_registry.py:89-123` (async) and `wager_registry.py:203-236` (sync)
**Confidence:** 0.85
**Risk:** Both functions use `INSERT OR IGNORE` followed by an unconditional `SELECT wager_id WHERE subsystem=? AND subsystem_id=?`. If the row already exists with stale values (different `wager_amount`, different `odds`, different `label`, or — most dangerous — registered by a different user via a snowflake collision attack), the function returns the existing `wager_id` AS IF the new write succeeded. The caller has NO way to detect that its values were silently dropped.
**Vulnerability:**
- No `cursor.rowcount` check after the `INSERT OR IGNORE` to detect "row already existed."
- No "did the inserted values match the requested values" verification.
- The composite UNIQUE key is `(subsystem, subsystem_id)` — there's no `discord_id` in the key, so a bug elsewhere (e.g., reused `subsystem_id` across users in PvP coinflip) would map a wager to the wrong user with zero warning.
- Callers like `polymarket_cog.py:734-735`, `casino_db.py:551-552`, `flow_sportsbook.py:1153-1155` have no way to know whether they wrote a fresh row or hit an existing one — both return the same wager_id with no second return value.
- Combined with the wallet-debit-after-register pattern in some call sites (e.g., `casino_db.py:1280-1285`), a transient failure during placement that retries would silently skip the registry write while still proceeding with a fresh wallet debit on the second attempt.
**Impact:** "Phantom" wagers — wagers that exist in the wallet ledger but show stale values in the registry — are produced silently. Cross-system reconciliation between `transactions` and `wagers` becomes unreliable, and disputes can't be resolved because the registry's "first-write-wins" semantics aren't documented and don't match caller expectations.
**Fix:**
1. After `INSERT OR IGNORE`, check `cursor.rowcount`. If 0, either (a) raise `WagerAlreadyRegisteredError(wager_id, existing_values)` so the caller can decide, or (b) return a tuple `(wager_id, was_new)` so the caller knows.
2. Document the "first-write-wins" semantics explicitly in the docstring.
3. Add an `UPSERT` variant for the legitimate "I want to overwrite if it exists" case.
4. Add a defensive check that the existing row's `discord_id` matches the requested `discord_id` before returning the wager_id; if not, raise `IdentityMismatchError`.

---

### WARNING #1: `payout_calc()` integer truncation diverges from live settlement payouts in backfill

**Location:** `wager_registry.py:387` (and same pattern at 419, 451)
**Confidence:** 0.80
**Risk:** Backfill computes `result_amount = payout_calc(amt, odds) - amt` for "won" wagers, but `payout_calc` (in `odds_utils.py:9-16`) does `int(wager + wager * (100 / abs(odds)))` — truncating with Python's `int()` floor rounding. Live settlement may have rounded differently (banker's rounding, or even used `round()` instead of `int()`), so backfilled `result_amount` will diverge from the actual ledger profit by 1-2 currency units per bet for non-multiple-of-110 odds.
**Vulnerability:**
- `get_wager_summary` SUMs `result_amount` to compute lifetime P&L. The reported P&L will not match the wallet ledger SUM.
- For users with thousands of historical bets, the divergence compounds into a noticeable mismatch ("ATLAS says I'm up $4823 but my wallet shows $4861").
- No reconciliation report or sanity check exists.
**Impact:** Lifetime P&L surfaces report wrong numbers; cross-system reconciliation is impossible without a manual joined query.
**Fix:** For backfill, prefer joining against the `transactions` table on `subsystem='SPORTSBOOK'` and `subsystem_id=str(bet_id)` to recover the actual settlement amount. Fall back to `payout_calc` only when no ledger entry exists.

---

### WARNING #2: `aiosqlite.connect()` calls have no timeout — async backfill can deadlock under load

**Location:** `wager_registry.py:120, 148, 188, 311, 323, 335, 364, 529, 592` (every async helper)
**Confidence:** 0.75
**Risk:** Every async call uses `aiosqlite.connect(DB_PATH)` with no `timeout=` argument. The default aiosqlite timeout is 5 seconds. Backfill functions write thousands of rows in a single transaction holding a write lock; a concurrent reader (e.g., a hub view query) hitting the same DB will block until lock release. If lock release exceeds 5s the reader raises `OperationalError("database is locked")`, which the calling cog's broad except may swallow into a blank embed.
**Vulnerability:**
- Sync helper `_db_con_sync()` uses `_DB_TIMEOUT = 10` (line 28, 198), but the async helpers ignore it.
- WAL mode helps reads pass writers, but `register_wager` / `settle_wager` are write-write and will still serialize.
- During the cold-start backfill window the entire DB is single-writer-locked.
**Impact:** Display surfaces (Flow Hub, sportsbook hub, prediction portfolio) silently render empty embeds for the first 10-30 seconds after restart while the backfill holds the write lock.
**Fix:** Pass `timeout=_DB_TIMEOUT` to every `aiosqlite.connect(DB_PATH, ...)` call. Bump `_DB_TIMEOUT` to 30 for backfill paths specifically.

---

### WARNING #3: `register_wager` writes are not gated by `wagers` table existence — startup race window

**Location:** `wager_registry.py:89-123, 203-236` and bot.py setup_hook ordering at `bot.py:241-289`
**Confidence:** 0.80
**Risk:** `bot.py` loads cogs (line 261-266) before calling `flow_wallet.setup_wallet_db()` (line 271), which is the only function that calls `ensure_wager_table`. Any cog that fires `register_wager_sync` from a `setup()` hook, or any background task started during `cog_load`, will hit `OperationalError: no such table: wagers`.
**Vulnerability:**
- `register_wager_sync` is called from many `flow_sportsbook` paths (e.g., line 1155, 1359, 1504); if any are reachable from `setup()`, they crash.
- `setup_wallet_db()` import-on-call style means `wagers` table creation is deferred until a step that's NOT first.
- `ensure_wager_table` is async-only (line 78), so even if a sync caller wanted to bootstrap the table on demand, it can't.
**Impact:** First-deploy reliability bug; a fresh DB or a deletion of `flow_economy.db` produces `OperationalError` on the first wager write during bot startup window.
**Fix:** Move `flow_wallet.setup_wallet_db()` (and the `ensure_wager_table` call inside it) BEFORE `for ext in _EXTENSIONS: bot.load_extension(ext)` in `bot.py`. Or add a sync `ensure_wager_table_sync()` and call it from `register_wager_sync` on first use.

---

### WARNING #4: `backfill_wagers` step 5 mishandles 'sold' status from `prediction_contracts` v2 schema

**Location:** `wager_registry.py:488-514`
**Confidence:** 0.85
**Risk:** `polymarket_cog.py:606-635` migrates `prediction_contracts` to a v2 schema with a new `'sold'` status (and `sell_price`, `sell_bucks`, `sold_at` columns). The backfill switch on lines 497-504 only handles `'won' | 'lost' | 'voided' | 'open'` — for `'sold'`, `result_amount` stays None and `settled_at` stays at the resolved column value, but the wager is inserted with `status='sold'` which is not in `VALID_TRANSITIONS` (lines 31-38) and not summed by `get_wager_summary` (lines 333-350).
**Vulnerability:**
- `VALID_TRANSITIONS` does not include `'sold'`, so `update_wager_status` would refuse to update a sold wager — likely irrelevant in practice but a contract gap.
- `get_wager_summary` SUMs `wins/losses/pushes` but `'sold'` rows show up in `total` with no contribution to W/L/P → user sees "100 total wagers" but only 87 W/L/P → confusion.
- `result_amount=None` for sold wagers means `SUM(COALESCE(result_amount, 0))` treats them as 0 P&L, hiding the actual cash extracted from the sell.
**Impact:** Prediction P&L is undercounted; sold contracts don't contribute to lifetime stats.
**Fix:** Add an `elif s == "sold": result_amount = (sell_bucks or 0) - cost` branch. Add `'sold'` to `VALID_TRANSITIONS` if you want it tracked, or normalize `'sold'` → `'won'` or `'lost'` based on profit sign. Also extend `get_wager_summary` to include `'sold'` in the bucket counts.

---

### WARNING #5: `get_wager_summary` includes `voided` wagers in `total_wagered`, inflating lifetime metrics

**Location:** `wager_registry.py:333-350`
**Confidence:** 0.78
**Risk:** The SQL `SUM(wager_amount) as total_wagered ... WHERE status != 'open'` includes wagers with `status='voided'` (and `'cancelled'`). Voided wagers were refunded — the user never lost the money — but they're counted in `total_wagered`, inflating the user's "lifetime risked" stat.
**Vulnerability:**
- No filter on `status NOT IN ('voided','cancelled')`.
- `total` count also includes voided wagers but `wins + losses + pushes` does not, so `total - (wins + losses + pushes) > 0` and the discrepancy leaks into UI tables that subtract.
- The `subsystem` grouping may show entries dominated by voided wagers, falsely suggesting heavy activity in (e.g.) `PROP` when most of those wagers were refunded.
**Impact:** User-visible lifetime metrics overstate actual risk and activity.
**Fix:** Add `status NOT IN ('voided', 'cancelled')` to the WHERE clause, and split out `voided` as its own column if you want it visible.

---

### WARNING #6: Backfill `outcome` mapping for `casino_sessions` collapses unknown outcomes to `'lost'`

**Location:** `wager_registry.py:475`
**Confidence:** 0.70
**Risk:** `s = "won" if outcome == "win" else ("push" if outcome == "push" else "lost")` — anything that isn't `'win'` or `'push'` becomes `'lost'`. But casino games actually use `'loss'` (singular) per `casino/games/slots.py:248`, `casino/games/crash.py:273`, `casino/games/coinflip.py:95`, `casino/games/blackjack.py:248`, etc. The string `'loss'` falls through to the else branch and is correctly mapped to `'lost'`. BUT — if a future game type uses an outcome like `'push'`, `'cashout'`, `'tie'`, `'jackpot'`, or `'active'` (the renderer at `casino/renderer/casino_html_renderer.py:1168` uses `'active'`), it gets silently mislabeled as a loss in the backfill.
**Vulnerability:**
- No explicit list of expected outcomes; no `else: log.warning(...)` branch.
- A single mistyped value coerces to a permanent "lost" record with `result_amount = payout - wager_amt` (which could be positive if the game was actually a win — silently corrupting the ledger).
- `result_amount` arithmetic uses the actual recorded `payout`, but `s` is derived from `outcome`, so a "win" with the wrong outcome string becomes a "lost" wager with positive `result_amount` — inconsistent.
**Impact:** Future game types or any historical row with an unexpected outcome string are silently mislabeled, polluting lifetime P&L.
**Fix:** Use an explicit map: `OUTCOME_MAP = {"win": "won", "loss": "lost", "push": "push"}; s = OUTCOME_MAP.get(outcome); if s is None: log.warning(...); continue`. Reject unknowns instead of forcing them to 'lost'.

---

### WARNING #7: `_db_con_sync()` sets `PRAGMA journal_mode=WAL` on every connection — wasteful and confusing

**Location:** `wager_registry.py:197-200` (and same pattern in `flow_wallet._db_con_sync`)
**Confidence:** 0.60
**Risk:** WAL mode is a persistent database-level setting, not per-connection. Setting it on every connection is wasteful and creates the false impression that this connection differs from others. The PRAGMA itself returns a row that's never consumed; under high call rates this is a measurable allocation cost.
**Vulnerability:** Performance smell more than correctness issue. But the duplicated pattern across files (`flow_wallet.py`, `wager_registry.py`, `flow_sportsbook.py`) suggests the WAL setup logic should live in one place.
**Impact:** Negligible performance overhead per call; non-zero contribution to startup latency under cold-start with many `register_wager_sync` calls.
**Fix:** Set `PRAGMA journal_mode=WAL` once during `setup_wallet_db()` and remove it from per-connection helpers. Document that WAL is a persistent file-level setting.

---

### WARNING #8: `_now()` ISO format and source-table timestamp formats are not consistently parseable downstream

**Location:** `wager_registry.py:46-47` and the many `or _now()` fallbacks throughout
**Confidence:** 0.65
**Risk:** `_now()` returns `datetime.now(timezone.utc).isoformat()` — produces something like `2026-04-09T14:23:11.234567+00:00` (with microseconds and explicit offset). But source tables use SQLite `CURRENT_TIMESTAMP`, which produces `2026-04-09 14:23:11` (no microseconds, no T separator, no offset). Backfilled rows therefore mix two formats inside a single `created_at` column. Any downstream code that tries to parse with a single format string will fail on one of them; sort-by-string-comparison still works because both forms are roughly lexicographically ordered, but `MIN`/`MAX` and `julianday()` may behave inconsistently.
**Vulnerability:**
- `played_at` from `casino_sessions` is stored as ISO via `now = datetime.now(timezone.utc).isoformat()` (live code), but `bets_table.created_at` uses `CURRENT_TIMESTAMP` (different format).
- Display layer that parses `created_at` may break on one form.
**Impact:** Sort order is inconsistent, time arithmetic is unreliable, and display surfaces may render mixed formats.
**Fix:** Normalize all timestamps to one canonical form during backfill. Either coerce SQLite `CURRENT_TIMESTAMP` strings to ISO via `datetime.fromisoformat()` parse-and-reformat, or keep both forms but expose a `_normalize_ts()` helper that the display layer uses.

---

### WARNING #9: `register_wager()` registry write is not transactionally bound to the wallet debit when called without `con=`

**Location:** `wager_registry.py:89-123` (`register_wager` async path) and call sites in `casino/casino_db.py:1280-1285`, `polymarket_cog.py:734-735`, `flow_sportsbook.py:1153-1155`
**Confidence:** 0.78
**Risk:** When `register_wager` is called without `con=`, it opens its own `aiosqlite.connect(DB_PATH)` and commits independently. This is a SEPARATE transaction from any wallet debit / ledger insert. If the process crashes between `register_wager` succeeding and the subsequent `flow_wallet.debit` succeeding (or vice versa), the registry and ledger are out of sync — a wager exists with no debit, or a debit exists with no registered wager.
**Vulnerability:**
- The function offers a `con=` parameter for joining a caller's transaction, but many call sites don't use it.
- There's no documentation that `con=` is REQUIRED for transactional consistency with the wallet.
- No reconciliation report exists between `wagers` and `transactions` tables.
**Impact:** Audit trail can disagree with the wallet ledger after process crashes; "phantom" wagers appear in lifetime stats with no matching debit.
**Fix:**
1. Document that `con=` is required for callers that need atomicity with their wallet operation.
2. Add a sentinel: emit a warning log when `register_wager` is called without `con=` so the discipline is visible in the log stream.
3. Add a periodic reconciliation task that joins `wagers` against `transactions` and reports orphans.

---

### OBSERVATION #1: Schema has no migration support — future column adds require manual DDL

**Location:** `wager_registry.py:54-75` (`_CREATE_TABLE` and `_INDEXES`)
**Confidence:** 0.95
**Risk:** Schema is defined as a single `CREATE TABLE IF NOT EXISTS` with no version tracking. Adding a new column (e.g., `event_id`, `correlation_id`, `parent_wager_id` for parlay legs) requires writing an `ALTER TABLE` migration manually outside this file, with no tracking of which migrations have run.
**Vulnerability:** Standard schema-evolution gap; not currently broken but will hurt future maintenance.
**Impact:** Schema drift between dev/staging/prod is hard to detect; new columns get added piecemeal in each cog.
**Fix:** Add a `_MIGRATIONS` list with `version → DDL` entries, track in a `wager_registry_meta(version INTEGER)` table, and run pending migrations in `ensure_wager_table()`.

---

### OBSERVATION #2: `result_amount INTEGER` will silently coerce float inputs without raising

**Location:** `wager_registry.py:64`
**Confidence:** 0.80
**Risk:** SQLite will silently coerce `result_amount=12.5` to `12.5` (REAL) into an INTEGER column without raising. Future callers from a partial-cashout path that uses fractional currency will write float values that downstream `SUM(result_amount)` returns as a float, breaking int-only consumers.
**Vulnerability:** Type promiscuity; not currently exercised but a future foot-gun.
**Impact:** Lifetime P&L could become a float in some queries and an int in others; display layer may render `1234.0` instead of `1234`.
**Fix:** Explicitly cast `int(result_amount)` in `register_wager` and `settle_wager` before binding the parameter, and document that fractional currency is not supported.

---

### OBSERVATION #3: `VALID_TRANSITIONS` does not include `'sold'` from prediction_contracts v2 schema

**Location:** `wager_registry.py:31-38`
**Confidence:** 0.85
**Risk:** The transition table covers `open / won / lost / push / voided / cancelled` but `polymarket_cog.py:618` introduced `'sold'` for partial-cashout contracts. `update_wager_status` will refuse any transition involving `'sold'`. As long as no caller actually uses `'sold'` here it's fine — but the gap is silent.
**Vulnerability:** Implicit contract gap. No test enforces it.
**Impact:** A future caller adding `'sold'` settlements will hit `InvalidTransitionError` and the bug will surface only at runtime.
**Fix:** Add `'sold'` to `VALID_TRANSITIONS` (e.g., `"open": {... "sold"}, "sold": {"voided"}`) and update `get_wager_summary` to include sold-bucket aggregation.

---

### OBSERVATION #4: `casino_sessions` `outcome='loss'` (singular) requires the implicit fallthrough to map correctly — fragile coupling

**Location:** `wager_registry.py:475`
**Confidence:** 0.70
**Risk:** All casino games use `outcome='loss'` (see `casino/games/slots.py:248`, etc.), but the wager registry uses `'lost'` (past tense) per `VALID_TRANSITIONS`. The translation works ONLY because the else branch on line 475 catches `'loss'` and maps it to `'lost'` by accident. If anyone refactors casino games to use `'lost'` directly, the code still works; if anyone refactors the else branch to be more strict (e.g., raise on unknown), the casino backfill breaks.
**Vulnerability:** Hidden contract; the test that would catch the regression doesn't exist.
**Impact:** Maintenance hazard; not a runtime bug today.
**Fix:** Make the mapping explicit: `OUTCOME_MAP = {"win": "won", "loss": "lost", "push": "push"}` so the relationship between source and target vocabularies is documented in code.

---

### OBSERVATION #5: Backfill summary lacks per-step counts in the return value

**Location:** `wager_registry.py:357-517`, `524-584`, `587-614`
**Confidence:** 0.65
**Risk:** All three backfill functions return a single `total: int`. Operators see "backfilled 47823 wagers" with no breakdown per source table. If one source is silently empty (e.g., the predictions step found 0 rows because of a JOIN bug), it's invisible.
**Vulnerability:** Observability gap.
**Impact:** Silent partial failures are not visible from the startup log.
**Fix:** Return a `dict[str, int]` like `{"TSL_BET": 1234, "PARLAY": 56, "PROP": 78, "CASINO": 9012, "PREDICTION": 345}` and log each.

---

### OBSERVATION #6: `_DB_TIMEOUT` constant is duplicated across modules

**Location:** `wager_registry.py:28` and `flow_wallet.py` (same constant)
**Confidence:** 0.55
**Risk:** Both files declare `_DB_TIMEOUT = 10` independently. Future tuning requires updating both, and they could drift. The same applies to `_db_con_sync()` (duplicated between `flow_wallet.py:514-518` and `wager_registry.py:197-200`).
**Vulnerability:** Convention drift.
**Impact:** Maintenance burden; no runtime bug.
**Fix:** Move the constant and the `_db_con_sync()` helper to a single shared `flow_db.py` module that both `flow_wallet` and `wager_registry` import. (`wager_registry` already imports `DB_PATH` from `flow_wallet` — extending that pattern is natural.)

## Cross-cutting Notes

Three patterns from this file likely affect the rest of the Flow/Economy ring:

1. **Early-exit guards on count instead of marker tables** — `backfill_wagers` is the second instance of this anti-pattern in the audit (the first was `flow_wallet.backfill_subsystem_tags` referenced in `bot.py:272`). Any backfill that uses `SELECT COUNT(*) > 0` as a "done" signal will silently break the moment live writes occur. Recommend a unified `backfill_runs(name TEXT PRIMARY KEY, completed_at TEXT)` table consumed by all backfill helpers.

2. **TOCTOU SELECT-then-UPDATE without `BEGIN IMMEDIATE`** — `update_wager_status` and `update_wager_status_sync` both exhibit this. Need to grep `flow_sportsbook.py`, `casino_db.py`, and `polymarket_cog.py` for the same pattern; settlement paths are particularly likely to share the bug.

3. **`register_wager` "first-write-wins" semantics undocumented** — Callers across `casino_db.py`, `polymarket_cog.py`, and `flow_sportsbook.py` invoke `register_wager` with the assumption that retries are safe (true) and that values are updated on retry (FALSE). Worth a one-line audit of every caller to confirm whether they tolerate the "stale row stays" behavior.

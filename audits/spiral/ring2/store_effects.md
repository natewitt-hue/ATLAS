# Adversarial Review: store_effects.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 247
**Reviewer:** Claude (delegated subagent)
**Total findings:** 14 (2 critical, 6 warnings, 6 observations)

## Summary

The module is small and mostly tight, but it sits at a dangerous intersection — it is the **documented public API for effect lifecycle** yet the primary writer (`flow_store._activate_item`) bypasses `activate_effect()` and writes raw INSERTs, leaving `MAX_STACK` unenforced in production. Beyond the drift, `consume_effect()` has a read-modify-write race on the `uses` counter, `get_active_effects()` mutates shared `store_effects` state on a read path without retry safety, and several SQL operations rely on implicit commits via the `with` block, which silently swallow write failures. Fix the enforcement drift with `flow_store.py` (or delete `MAX_STACK` so the contract matches reality) and harden `consume_effect` before relying on limited-use items in production.

## Findings

### CRITICAL #1: `MAX_STACK` cap is not actually enforced in production — `activate_effect()` is dead code on the hot path
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:205-231`
**Confidence:** 0.98
**Risk:** The BUG-10 "fix" in this file is a lie. The real writer of `store_effects` rows is `flow_store._activate_item()` (see `flow_store.py:443-454`), which does a raw `INSERT INTO store_effects` and never calls `activate_effect()`. The MAX_STACK=10 guard lives inside `activate_effect()` (lines 218-224) which is never invoked by the activation path. A grep across the codebase confirms no non-test caller of `activate_effect()` exists.
**Vulnerability:** The only other layer that could prevent stacking is flow_store's own check at `flow_store.py:408-421`, but that check is `count > 0` ("no stack at all"), a fundamentally different contract from "cap stack at 10". If that `count > 0` check is ever relaxed — even accidentally, e.g., to allow two simultaneous xp boosts — MAX_STACK will not fire because nobody calls the helper that enforces it. The BUG-10 fix comment in this file gives false confidence to reviewers that stacking is bounded. It is not.
**Impact:** Silent regression waiting to happen. A future dev who reads the CLAUDE.md note "MAX_STACK=10 enforced in activate_effect" will assume the guardrail is live and relax the no-stack rule in flow_store. At that moment a user can dupe-activate effects until `store_effects` is multi-million rows per user, blowing up `get_active_effects()` response time and balance math in `get_multiplier()`. Also: `get_multiplier()` explicitly takes `effects[0]` (line 115), silently ignoring stacks 2..N, which rewards duping with free boosts that never expire from the multiplier calc.
**Fix:** Either (a) delete the unused `activate_effect()` and the MAX_STACK constant and make flow_store's guard the single source of truth, or (b) refactor `flow_store._activate_item()` to call `store_effects.activate_effect()` inside the same transaction. Option (b) is correct per the ring 1 flow_store finding but requires threading the aiosqlite connection through, since `activate_effect()` currently opens its own sync connection. At minimum, rename the comment from "BUG-10 FIX" to "NOT WIRED — see flow_store._activate_item for the live path" so the next reviewer isn't misled.

### CRITICAL #2: `consume_effect()` has a read-modify-write race on the `uses` counter
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:131-177`
**Confidence:** 0.9
**Risk:** The function reads `effect_data` in a SELECT (lines 141-146), parses `uses`, decrements it in Python (line 158), and then writes it back with an UPDATE (lines 160-169). There is no `BEGIN IMMEDIATE`, no `WHERE effect_data=?` version check, and the `with _db_con() as con` context only commits on exit — it does not lock the row for the duration of the read-modify-write. Two concurrent consume calls (double-click on a "use reroll" button, two quick `/reroll` interactions, an admin sim and a user click) will both read `uses=3`, both write `uses=2`, and the user gets two free rerolls.
**Vulnerability:** This is a textbook TOCTOU on a limited-use resource. The only serialization at this layer is SQLite's WAL-level writer lock, but readers don't take it. Since both callers execute the SELECT before the UPDATE, both see the same stale `uses` value, and the last writer simply overwrites the first without detecting the conflict. The flow_store path uses `get_user_lock(discord_id)` (see `flow_store.py:374`), but `consume_effect()` is synchronous and does not take that lock — so any caller invoking `consume_effect()` outside `_activate_item()`'s lock (which is the typical use case, since consume runs on reroll/insurance hot paths) races freely.
**Impact:** Economy exploit: users duplicate "uses" on limited-use items (reroll, insurance, second chance). Severity scales with how many uses get duped per click — at the extreme, fast clickers can drain a 10-use reroll as 20 rerolls. No ledger trail, no detection.
**Fix:** Wrap the read-modify-write in `BEGIN IMMEDIATE` ... `COMMIT`, OR use an atomic `UPDATE store_effects SET effect_data = json_set(effect_data, '$.uses', json_extract(effect_data, '$.uses') - 1) WHERE effect_id=? AND json_extract(effect_data, '$.uses') > 0` and check `cur.rowcount == 1`. Also add a `get_user_lock` equivalent for sync callers, or force `consume_effect()` to be called only inside the caller's lock.

### WARNING #1: `get_active_effects()` silently swallows the inline-expire UPDATE failure
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:56-96`
**Confidence:** 0.8
**Risk:** The function is documented as a read, but it mutates `store_effects` inline (sets `is_active=0` for expired rows at lines 88-94). The UPDATE runs inside the same `with _db_con() as con` block as the SELECT. If the UPDATE fails (lock timeout, disk full, WAL contention), the exception will bubble out — **but** if it succeeds partially in one of many concurrent readers and then another reader reads the same rows as "active" before the first UPDATE commits, both will return expired effects as active. There is no `BEGIN IMMEDIATE` around the read+write pair.
**Vulnerability:** Read paths should not mutate. The "expire inline while reading" pattern introduces:
  1. Writer starvation — every read path (every `has_effect`, `get_multiplier`, `get_badges_and_flair`) can issue a write.
  2. A narrow window where an effect is past `expires_at` but not yet updated — and `get_multiplier` will read it as active (line 77 filter `row["expires_at"] < now`) is per-row, not atomic with the UPDATE.
  3. On slow disks, the UPDATE can raise `sqlite3.OperationalError: database is locked` because two simultaneous readers both try to UPDATE. The outer `with con:` block re-raises, which means every hot-path read can fail with a DB error.
**Impact:** Intermittent `OperationalError` flaring on `/atlas`, `/profile`, slot spins, sportsbook placement — wherever `get_active_effects` runs. Hard to reproduce locally, shows up in production as "random Discord errors".
**Fix:** Move the inline expiration to a background task that calls `expire_stale_effects()` periodically, and make `get_active_effects()` a pure read: `SELECT ... WHERE is_active=1 AND (expires_at IS NULL OR expires_at >= ?)`. This lets SQLite handle the filter atomically and eliminates the writer-on-read pattern.

### WARNING #2: `get_active_effects()` returns corrupted JSON rows silently
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:80-85`
**Confidence:** 0.85
**Risk:** If `effect_data` in the DB is corrupted or not valid JSON, the function catches `JSONDecodeError` and substitutes `{}`. The row is still included in results as if it were valid. This means `get_multiplier()` will then call `data.get("bonus_pct", 0)` and return 1.0 — the caller has no way to know the effect row is broken, the user sees no boost, and the row is still marked active in the DB (because it's not "expired").
**Vulnerability:** Silent data corruption. No log line, no metric, no alert. A malformed insert (e.g., a bug in the BUG-10 "fix" path writing `effect_data` as a Python repr instead of JSON) goes undetected until users complain their purchased boost "does nothing".
**Impact:** Customer support ticket black hole — user paid for a boost, the row exists in `store_effects`, `is_active=1`, but the boost silently returns 1.0. Refund disputes with no audit trail.
**Fix:** `log.error("store_effects row %d has invalid effect_data JSON: %r", row["effect_id"], row["effect_data"])` inside the except block. Consider also marking such rows as `is_active=0` so they don't keep triggering on every read.

### WARNING #3: `activate_effect()` has no transaction around the count-check → insert pair
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:217-231`
**Confidence:** 0.85
**Risk:** The SELECT COUNT(*) (lines 218-222) and INSERT (lines 225-230) are two separate statements inside the same `with _db_con() as con` block, but there is no `BEGIN IMMEDIATE`. Two concurrent callers can both read `count=9`, both conclude "under the cap", and both insert — yielding 11 rows. The MAX_STACK check is non-atomic.
**Vulnerability:** Same TOCTOU pattern as consume_effect. The `with` block commits on exit but doesn't serialize read+write as one transaction. Since this is the documented path for inserting effects (even though flow_store bypasses it today), it must be safe under concurrency. It isn't.
**Impact:** Even if finding #1 is fixed and flow_store starts calling `activate_effect()`, the MAX_STACK cap can still be exceeded under concurrent activations. For duplicative button-click activations (which are common in Discord views), this means the cap is approximate, not hard.
**Fix:** Wrap in an explicit transaction: `con.execute("BEGIN IMMEDIATE")`, then count, then insert, then `con.commit()`. Or use a unique-index approach: a UNIQUE index on `(discord_id, effect_type, started_at)` with an `INSERT ... ON CONFLICT DO NOTHING` and count-check-after. A CHECK constraint or trigger can also enforce the cap at DB level.

### WARNING #4: Connection pattern opens a fresh SQLite connection per call with no pool
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:31-35, 56, 139, 217, 241`
**Confidence:** 0.7
**Risk:** Every function opens a fresh `sqlite3.connect(DB_PATH, timeout=10)`. On a hot read path like `get_multiplier` (called on every casino payout calculation) or `has_effect` (called on slot spin evaluations), this is a non-trivial overhead on Windows where file-handle open/close is slow. Worse, every call also runs `PRAGMA journal_mode=WAL` — PRAGMA journal_mode is session-level, but re-running it on every connection is a small waste and will fail noisily if another connection is already in a transaction (SQLite returns "database is locked" on the PRAGMA in rare cases).
**Vulnerability:** Not a correctness bug, but a scalability hazard. Under burst load (slots event, casino night), every spin opens and closes a connection; connection exhaustion or file-lock contention can spike latency and cascade into user-visible lag.
**Impact:** Latency spikes in casino / sportsbook hot paths. Harder-to-diagnose "everything is slow" incidents.
**Fix:** Either (a) take an optional `con` parameter on each function so callers can share their transaction (this also fixes the atomicity problems in #1, #3), or (b) use a thread-local connection or a connection pool. Alternatively, since all functions are synchronous and CPU-cheap, batch them behind `asyncio.to_thread()` with a cached module-level connection.

### WARNING #5: All functions are synchronous but called from async cogs — blocks the event loop
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:1-247` (module-wide)
**Confidence:** 0.8
**Risk:** Per the docstring "All functions are synchronous (SQLite single-row lookups are <1ms)". But the module's blocking sqlite3 calls run on the Discord event loop whenever called from a cog without `asyncio.to_thread()`. Under concurrency (multiple users spinning slots simultaneously), each SELECT + possible UPDATE blocks the bot for as long as SQLite takes to acquire the WAL writer lock — which, per the ATLAS focus block, "blocking calls inside async functions" is flagged as a hard rule violation.
**Vulnerability:** The "under 1ms" claim is false when inline expiration fires in `get_active_effects()` — the UPDATE takes the writer lock. Under WAL contention from other writers (flow_store activation, casino payouts), the SELECT + UPDATE can block for the full `_DB_TIMEOUT=10` seconds, locking the entire bot event loop.
**Impact:** Bot-wide latency cliff during busy periods. Other cogs (sentinel, oracle) will appear to hang because the event loop is blocked on a sqlite3 call from `has_effect`.
**Fix:** Either (a) make all functions async and use `aiosqlite` (matching `flow_store.py`), or (b) document clearly that all callers MUST wrap in `asyncio.to_thread(...)` and update every existing caller. Given the ATLAS codebase standard uses `aiosqlite` in flow_store, (a) is the correct answer.

### WARNING #6: `_utcnow()` returns a string, enabling lexical-compare bugs
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:38-40, 77, 244`
**Confidence:** 0.65
**Risk:** `_utcnow()` returns `"YYYY-MM-DD HH:MM:SS"` — a string. All comparisons against `expires_at` (lines 77, 244) are string lexical comparisons. This works **only** if every write to `expires_at` also uses the exact same `YYYY-MM-DD HH:MM:SS` format. In `flow_store.py:427-428`, the format matches — but any other writer that inserts `expires_at` with ISO 8601 `T` separator (e.g., `datetime.isoformat()`) or with microseconds will break lexical ordering silently. The DB column is `TIMESTAMP` which SQLite does not enforce, so anything is accepted.
**Vulnerability:** `'2026-04-09T12:00:00' < '2026-04-09 12:00:00'` is FALSE in string comparison (`T` > space), so an ISO-formatted `expires_at` will never appear expired.
**Impact:** Permanently un-expiring effects when any third writer uses a different format. Latent bug waiting for a refactor.
**Fix:** Store `expires_at` as a Unix epoch integer (or use SQLite's `strftime('%s', ...)`) and compare numerically. At minimum, add a docstring asserting the exact format and a single helper that all writers must use.

### OBSERVATION #1: `get_multiplier()` ignores stacks beyond index 0
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:115`
**Confidence:** 0.9
**Risk:** `eff = effects[0]` silently picks the first effect and ignores any others. The comment says "V1: no stacking", but this is inconsistent with MAX_STACK=10 allowing up to 10 effects. If MAX_STACK>1 ever means "stack multipliers", the math at line 120/123 silently drops 9 of them.
**Vulnerability:** Contract mismatch between `activate_effect()` (allows stack of 10) and `get_multiplier()` (uses only one). Fine for now, but "V1" comments age badly.
**Impact:** Future bug surface — developer assumes stacking works because MAX_STACK=10 is advertised.
**Fix:** Either reconcile by summing / maxing / multiplying all active effects in the list, or reduce MAX_STACK to 1 to match the no-stack semantics flow_store actually enforces.

### OBSERVATION #2: `get_multiplier()` bonus_pct contract is undocumented
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:118-123`
**Confidence:** 0.7
**Risk:** `sportsbook` uses `bonus_pct` (percentage) while `casino`/`xp` use `multiplier` (direct). Two different keys, two different units, no docstring explanation. A dev authoring a new sportsbook boost item must read the function body to know whether to write `bonus_pct=10` (for +10%) or `multiplier=1.10`.
**Vulnerability:** Easy mistake on new items — a casino multiplier `1.5` applied to a sportsbook effect becomes `1.0 + 1.5/100 = 1.015`, silently giving almost no boost instead of the expected 50%.
**Impact:** Silent boost misconfigurations.
**Fix:** Document the expected keys and units in the function docstring, and add `json_schema` validation at item-definition time to ensure the right key is set per category.

### OBSERVATION #3: `get_badges_and_flair()` issues 3 separate DB roundtrips
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:180-202`
**Confidence:** 0.8
**Risk:** Three calls to `get_active_effects()`, meaning 3 fresh SQLite connections, 3 PRAGMA runs, and 3 read-modify-write inline-expire passes, all for a single profile render. On the `/atlas` profile hot path, this triples the cost.
**Vulnerability:** Performance smell; not a correctness bug. But combined with finding #4 (connection per call), a single profile render can open 3 connections back-to-back.
**Impact:** Slower `/atlas` profile card rendering, especially for users with many effects.
**Fix:** Add a single `get_active_effects_bulk(discord_id, effect_types=[...])` helper that takes a list, runs one SELECT with `effect_type IN (?,?,?)`, and returns a dict keyed by effect_type.
**Note:** This touches finding #2 in flow_store ring 1 about `get_badges_and_flair` being called from hot paths.

### OBSERVATION #4: `has_effect()` wastes a JSON decode per call
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:126-128`
**Confidence:** 0.7
**Risk:** `has_effect` wraps `get_active_effects()` and only checks `len(...) > 0`. But `get_active_effects()` does `json.loads()` on every row (line 82), which `has_effect` then discards. For a simple existence check on a potentially large result set, the JSON parse is wasted work.
**Vulnerability:** Not a correctness bug, just wasted CPU on a potentially hot path (sportsbook edge check, casino boost check).
**Impact:** Small latency penalty, scales with result set size.
**Fix:** Add a dedicated SQL EXISTS query: `SELECT 1 FROM store_effects WHERE discord_id=? AND effect_type=? AND is_active=1 AND (expires_at IS NULL OR expires_at >= ?) LIMIT 1`. Returns in under 0.1ms, no JSON parse.

### OBSERVATION #5: `expire_stale_effects()` ignores `is_active=0` rows that should be purged
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:234-247`
**Confidence:** 0.6
**Risk:** The function marks rows inactive but never deletes them. Over time, `store_effects` grows unboundedly as every expired boost leaves a dead row. There is no retention / purge policy in this file, and the ring 1 flow_store audit did not mention a purge task elsewhere.
**Vulnerability:** Unbounded table growth → slower reads on the indexed `(discord_id, effect_type, is_active)` index over months as inactive rows pile up. Not an immediate bug; a cleanup debt.
**Impact:** DB bloat over months of operation. Eventually `flow_economy.db` becomes large enough to slow down every cog that opens it.
**Fix:** Add a nightly purge: `DELETE FROM store_effects WHERE is_active=0 AND started_at < date('now', '-90 days')`. Call from the store cog's daily task.

### OBSERVATION #6: No type hints on return shape; docstring lies about `get_active_effects`
**Location:** `C:/Users/natew/Desktop/discord_bot/store_effects.py:48-96, 180-202`
**Confidence:** 0.6
**Risk:** `get_active_effects` declares `-> list[dict]` but each dict is a hybrid: most fields are raw DB row values (int, str, or None) while `effect_data` is pre-parsed JSON (dict). No `TypedDict` defines the shape, and a consumer reading the return value has to know magic keys like `expires_at` vs `effect_data.bonus_pct`. Similarly, `get_badges_and_flair`'s return dict has a docstring but the actual shape of `badges`/`trophies` entries is whatever JSON was stored — could be anything.
**Vulnerability:** Contract drift between writers and readers. A dev adding a new field to `effect_data` has no place to declare it, so typos in readers go undetected.
**Impact:** Refactor hazard. Not a bug today.
**Fix:** Define `TypedDict`s for `ActiveEffect` and `BadgeData`/`TrophyData`/`FlairData` and use them in return annotations. Add a runtime schema check on activation.

## Cross-cutting Notes

- **Sync/async split is a broader architectural issue.** This module is explicitly synchronous while its only real caller (`flow_store.py`) is fully async with `aiosqlite` and per-user locks (`get_user_lock`). Any function in this file called from an async cog either blocks the event loop or has to be wrapped in `asyncio.to_thread()`, and the latter breaks `get_user_lock` semantics. Recommend converting this module to async and making `activate_effect()` accept an optional `db` connection so it can participate in the caller's transaction — which also resolves finding #1.
- **The TOCTOU pattern in `consume_effect` and `activate_effect` is identical** to the ones flagged in the ring 1 `flow_store` review — the file is missing any `BEGIN IMMEDIATE` / row-lock discipline. Same fix pattern applies everywhere: wrap read-modify-write in explicit transactions or use atomic `json_set`/`json_extract` SQL.
- **Connection-per-call hot-path pattern** (finding #4) likely exists in other utility modules in the ring 2 batch. Worth sweeping.
- **Silent JSON decode fallback** (finding #2) is a pattern — same except-and-substitute-empty-dict logic appears twice in this file (lines 83-84, 153-154). If the same pattern is in flow_store, it's worth a cross-cut fix that logs + marks the row as corrupt.

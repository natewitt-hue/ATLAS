# Adversarial Review: flow_store.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 716
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (3 critical, 8 warnings, 6 observations)

## Summary

The Phase 1 store engine has the right shape — per-user lock, BEGIN IMMEDIATE, debit/credit through `flow_wallet`, ledger writes — but it ships with three financial-grade defects: (1) the purchase `reference_key` includes a per-call `uuid4` so every retry generates a *new* key and double-debits; (2) stock decrement runs *after* the debit but in the same transaction without the rollback being acknowledged in the `new_balance`/`inventory_id` returned to callers, leaving the function returning success-shaped state from a rolled-back transaction; and (3) lootbox opening reads `flow_wallet.get_balance()` and writes `won_item_id` *outside* `BEGIN IMMEDIATE`, so two concurrent opens can race past `max_per_user`. The activation engine and effect-stack contract also drift from `store_effects.activate_effect()`, which is the only enforcer of `MAX_STACK`. None of this is currently exercised at runtime (no UI in Phase 1), but it WILL detonate the moment Phase 2 wires a button to it.

## Findings

### CRITICAL #1: Purchase `reference_key` is non-idempotent — every retry double-debits

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:202`
**Confidence:** 0.98
**Risk:** A Discord button double-click, an interaction retry, a network blip causing the caller to re-invoke `_purchase_item()` — any of these will cause the user to be charged twice and credited two inventory rows for the same logical purchase.
**Vulnerability:** The reference key is built as `f"store_purchase_{discord_id}_{item_id}_{uuid.uuid4().hex[:8]}"`. Because `uuid.uuid4()` is generated *inside* `_purchase_item()`, every call — including a retry of the same logical purchase — gets a fresh UUID. The whole point of `reference_key` per CLAUDE.md is to be deterministic across retries: "ALL debit calls MUST pass `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits." The format spec is `f"{SUBSYSTEM}_DEBIT_{uid}_{event_id}_{int(time.time())}"` — where `event_id` is a stable identifier for the logical event, NOT a fresh nonce. With a fresh UUID, `_check_idempotent()` in `flow_wallet.py:174` will *never* find a match, the debit will execute again, and a second inventory row will be inserted.
**Impact:** Direct financial loss for users on every retry. Inventory pollution. Ledger duplication. This is the exact scenario the CLAUDE.md "Flow Economy Gotchas" table calls out as the canonical violation.
**Fix:** Either (a) accept `reference_key` as a parameter from the Phase 2 caller (which knows the interaction ID — `interaction.id` is stable across the 3 retry attempts Discord makes), or (b) derive it from caller-provided `event_id`/`interaction_id`. Example: `_purchase_item(discord_id, item_id, *, idempotency_key: str)` and inside, `ref_key = f"STORE_PURCHASE_{discord_id}_{item_id}_{idempotency_key}"`. The Phase 1 code as written guarantees the bug ships the moment Phase 2 plugs in a button.

---

### CRITICAL #2: Stock-rollout rollback returns stale `new_balance` and `inventory_id`

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:316-359`
**Confidence:** 0.95
**Risk:** When two users race for the last unit of a limited-stock rotation, the loser's transaction rolls back the debit and inventory insert via `db.rollback()` on line 326 — but the function still returns `{"ok": False, "error": "Sold out!"}` with NO acknowledgement that the surrounding txn-scoped variables (`new_balance`, `inventory_id`) are now invalid. Worse, because `new_balance` was assigned BEFORE the rollback (line 295), the subsequent `return` statement on lines 352-359 is never reached on the sold-out path — but if any other code is added between the rollback and the early return, it could read stale post-debit state.

A more concrete, current bug: on line 326 the rollback happens AFTER the `INSERT INTO store_inventory` and AFTER the debit. The rollback reverses both — that part is correct — but the `flow_wallet.debit()` call on lines 295-304 has already inserted a row into the `transactions` table with the user's `reference_key` *under the same connection*. SQLite's BEGIN IMMEDIATE transaction does roll that back, but **nothing rolls back the in-memory `_check_idempotent()` cache** (there isn't one, but if you ever add one, this fails). More urgent: the early-return path on line 327 never inserts into `store_transactions`, so the ledger has no record of the failed-purchase attempt — there's no audit trail for "user tried to buy the last X and lost the race." Compliance and debugging visibility are gone.
**Vulnerability:** Mixing transaction control flow with return value construction in the same function makes it easy to leak post-debit state through a sold-out path. The function is correct on the happy path but the failure paths are under-tested by construction.
**Impact:** No ledger trail for failed purchases. If `_check_idempotent()` ever caches at the application layer, the rollback won't be visible. Race losers see "Sold out" but the Phase 2 UI has no way to reconcile if their balance briefly flickered (though SQLite WAL prevents that for the actual reader). Mostly observability and future-proofing risk, but enough to flag.
**Fix:** Restructure: do the stock decrement *before* the debit. Move the `UPDATE store_rotations ... WHERE stock_remaining > 0` check to *before* `flow_wallet.debit()` on line 295. If `cur.rowcount == 0`, return "Sold out!" without ever debiting. Then debit + insert inventory + insert ledger row. This eliminates the rollback-after-debit path entirely and means `flow_wallet`'s reference_key is never burned on a failed purchase.

---

### CRITICAL #3: Lootbox `max_per_user` check is TOCTOU — two concurrent opens can both see "not dupe"

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:563-584`
**Confidence:** 0.92
**Risk:** Two simultaneous lootbox opens (e.g., user double-clicks "Open" or two boxes open in rapid succession) both read `COUNT(*)` of the won item *before* `BEGIN IMMEDIATE` is started on line 586. Both see `owned < max_per_user`, both proceed to award the item, and the user ends up with one MORE than `max_per_user` allows. The `is_dupe` flag is set on line 583 inside the SAME `aiosqlite.connect()` block but BEFORE any transaction is opened — so the read is dirty.

Note that the per-user `get_user_lock()` on line 503 *does* serialize two opens for the SAME user, which mitigates this for a single user double-clicking. But the lootbox `inventory_id` is the same lock granularity, so if two DIFFERENT lootboxes (different `inventory_id`s) open and both can win the same restricted item, the per-user lock still serializes them — so this is mitigated. The remaining concern is the broader transaction isolation: the random selection (lines 543-558), dupe-check (575-583), and "award the item" INSERT (615-621) all happen across the boundary of `BEGIN IMMEDIATE` — the SELECT runs in autocommit mode, then `BEGIN IMMEDIATE` starts a fresh transaction. There's a window between the SELECT and the INSERT where another writer (e.g., admin grant, an unrelated purchase) could insert a row that should have made `is_dupe=True`.
**Vulnerability:** Read-then-modify pattern split across two distinct transaction boundaries (autocommit reads on lines 567-583, then a new explicit transaction on line 586). The per-user lock helps but does not cover writes from other code paths (admin grants, sportsbook payouts that grant items, lootbox-from-lootbox cascades).
**Impact:** Item-cap evasion. A user can hold more of a `max_per_user`-restricted item than the cap allows, breaking item economy. Particularly bad for one-of-a-kind items (`max_per_user=1`).
**Fix:** Move the won-item lookup, dupe-check, and inventory row read INSIDE the `BEGIN IMMEDIATE` block on line 586. Re-check `owned < max_per_user` after acquiring the write lock. If the cap has been hit between the random pick and the transaction acquire, fall through to the dupe coin payout.

---

### WARNING #1: Activation engine bypasses `store_effects.activate_effect()` — `MAX_STACK` cap is silently violated

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:445-454`
**Confidence:** 0.95
**Risk:** `_activate_item()` writes directly to `store_effects` via raw `INSERT INTO`, completely bypassing `store_effects.activate_effect()`, which is the function that enforces `MAX_STACK = 10` (see `store_effects.py:213-231`). The flow_store version has its own no-stack check on lines 408-421 — but its check is `count > 0` ("you already have an active X effect"), which is mutually exclusive with the helper's check (`count >= 10`). They disagree on the contract.
**Vulnerability:** Two enforcement layers with different rules. If anything ever changes the no-stack rule in `_activate_item()` (e.g., to allow 2 simultaneous casino multipliers), the `MAX_STACK=10` cap won't apply because the activate_effect helper isn't being called. Worse, `store_effects.activate_effect()` is the documented public API, and ignoring it means future migrations to that helper (e.g., adding telemetry or audit) will not touch this path.
**Impact:** Drift between the documented effect-insertion API and the actual code path. Any future work on `store_effects` that assumes all writes go through `activate_effect()` will be wrong.
**Fix:** Replace the raw INSERT on lines 445-454 with `store_effects.activate_effect(discord_id, inv["item_id"], effect_type, effect_data_str, now, expires_at)`. Note that this is a sync function and `_activate_item()` is async — wrap in `await asyncio.to_thread(...)`. Also, since the helper opens its own sqlite connection, it cannot participate in the existing `BEGIN IMMEDIATE` transaction — which means activation is no longer atomic with the inventory update. That's a worthwhile tradeoff: extract a `_activate_effect_in_txn(con, ...)` async helper that takes the existing `db` connection and reproduces the cap check inside the open transaction.

---

### WARNING #2: `_activate_item()` reads `effect_data` from the catalog, ignoring per-purchase customization

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:401-405`
**Confidence:** 0.85
**Risk:** The activation engine pulls `effect_data` from `store_items.effect_data` (catalog), not from `store_inventory.effect_data` (per-purchase). `store_inventory` has no `effect_data` column, which makes this design implicit. If an admin EVER updates `store_items.effect_data` (e.g., shortens duration, lowers multiplier) AFTER a user buys but BEFORE they activate, the user receives the *new* effect_data, not what they paid for. Conversely, if the admin sweetens the deal post-purchase, users get the upgrade for free.
**Vulnerability:** Catalog mutation between purchase and activation is undetectable. There's no snapshot of `effect_data` at purchase time.
**Impact:** Refund/dispute risk. Players who paid for "2x casino multiplier for 24h" find their boost has been silently nerfed to "1.5x for 12h" because admin fiddled with the catalog yesterday. Compliance issue if real money is involved.
**Fix:** Either (a) snapshot `effect_data` into `store_inventory` at purchase time (add `effect_data_snapshot TEXT` column, write it in `_purchase_item()` line 307-312), or (b) document that catalog changes apply retroactively to unactivated items and add a versioning column to detect drift. Option (a) is the safer default.

---

### WARNING #3: `_open_lootbox()` lootbox payout uses `random.choices()` — not cryptographically random, easily replayed

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:543-558`
**Confidence:** 0.75
**Risk:** Lootbox selection uses Python's `random` module, which is deterministic and seeded from process state. An attacker who can observe several lootbox openings in sequence could (in principle) reconstruct the Mersenne Twister state and predict future rolls. More practically: if the bot restarts, `random.seed()` defaults to system time at process start — under high parallelism, sequential lootbox opens within the first few microseconds after start could produce correlated rolls.
**Vulnerability:** `random.choices()` is `random.SystemRandom`-naive. The `random` module is not safe for any payout where the user has a financial incentive to predict it.
**Impact:** In a closed Discord server with trusted users, low practical risk. But this is a fintech-adjacent system per CLAUDE.md ("Financial blast radius: like flow_sportsbook, this is real money. Be thorough."). The fact that lootbox state is observable through Discord events means an attacker has read access to outputs.
**Fix:** Use `secrets.choice()` (with weighting via cumulative weights + `secrets.randbelow()`) or `random.SystemRandom().choices()`. Acceptable cost — only matters on lootbox opens, not on a hot path.

---

### WARNING #4: `coins_fallback` defaults are hardcoded as 1000-3000 — silently dictates fallback economy

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:600-602`
**Confidence:** 0.80
**Risk:** If `loot_data.get("coins_fallback", {})` returns an empty dict (e.g., the item author forgot to specify it), the lootbox awards 1000-3000 coins by default. This is a HARDCODED economy parameter buried in source code, not a config value. If TSL ever wants to scale the lootbox economy (e.g., season inflation), this number is invisible to admins until they grep the codebase.
**Vulnerability:** Magic numbers embedded in payout logic. No DB-side default. No central config.
**Impact:** Economic drift. If TSL doubles starting balance to 2000 next season, lootbox dupe fallback is now relatively *less* valuable, but no one notices because it's hidden in source.
**Fix:** Move defaults to a constants module or to `store_items.effect_data` JSON validation (require `coins_fallback` to be present, raise on activation if missing). At minimum, log a WARNING when an item has no `coins_fallback` and is opened, so the missing config is observable.

---

### WARNING #5: `_purchase_item()` `cooldown_hours` check uses `MAX(purchased_at)` but `purchased_at` is wall-clock TIMESTAMP DEFAULT CURRENT_TIMESTAMP — UTC mismatch

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:131, 273-289`
**Confidence:** 0.85
**Risk:** `store_inventory.purchased_at` defaults to `CURRENT_TIMESTAMP`, which in SQLite is **server-local time, NOT UTC**. The cooldown check on line 273 builds `cutoff = (_utcnow_dt() - timedelta(hours=...)).strftime("%Y-%m-%d %H:%M:%S")` — this is a UTC string. Comparing UTC strings to local-time strings via `purchased_at > ?` will give wildly wrong results depending on the deploy timezone. For PT (UTC-8), a 1-hour cooldown could become a 9-hour cooldown or a -7 hour "always cool" cooldown.
**Vulnerability:** SQLite `CURRENT_TIMESTAMP` is documented as UTC, BUT only in the strict `SQLITE_DEFAULT_AUTOMATIC_INDEX` build. In Python's bundled sqlite3 module, `CURRENT_TIMESTAMP` returns UTC (per SQLite docs), so this *might* work — but the codebase already has `_utcnow()` because `BUG-11 FIX: always use timezone.utc — never datetime.now() without tz` suggests at least one bug class around this. Inconsistent: the `started_at` for `store_effects` uses `_utcnow()` (line 452 — explicit), but `purchased_at` uses `CURRENT_TIMESTAMP` (column default, line 131). One should be wrong.
**Impact:** Cooldown enforcement is timezone-fragile. On a fresh deploy or VPS migration to a non-UTC host, cooldowns silently break.
**Fix:** Either (a) explicitly write `purchased_at=?` with `_utcnow()` in the INSERT on line 307, or (b) verify SQLite's `CURRENT_TIMESTAMP` returns UTC on the production build and document it. Option (a) is consistent with the rest of the file's `_utcnow()` discipline.

---

### WARNING #6: `_get_store_message_id` casts arbitrary text to int with no error handling

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:687-695`
**Confidence:** 0.90
**Risk:** `int(row[0]) if row else None` — if the stored value is malformed (manual DB edit, schema corruption, partial migration), this raises `ValueError` and crashes the caller. There is no try/except.
**Vulnerability:** Defensive coding gap. The function trusts that whatever was written via `_set_store_message_id()` round-trips cleanly, but the `sportsbook_settings` table is a key-value store shared with other subsystems.
**Impact:** Crash on Phase 2 panel-restore startup if the value is ever corrupted.
**Fix:** Wrap in `try: return int(row[0]); except (ValueError, TypeError): log.exception(...); return None`.

---

### WARNING #7: `_open_lootbox()` chooses an item but does not weight by `coins_fallback` for dupes — missing rarity pity

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:546-558`
**Confidence:** 0.70
**Risk:** The "guaranteed_rarity" pity loop tries 10 random picks, then falls back to "highest rarity in pool" via `max(...)` on line 558. This `max()` call assumes the pool always has at least one entry — it does (the `if not pool` check on line 533 guarantees nonempty), but the *deterministic* fallback means a player who hits the 10-attempt cap *always* gets the same single highest-rarity item every time. That's predictable and exploitable: trigger the pity loop, get the legendary 100% deterministically.
**Vulnerability:** The fallback is not random — it's `max()`, which is deterministic given the same pool ordering. A user who knows the pool can identify the "guaranteed" item.
**Impact:** Lootbox value floor is exploitable. If the highest-rarity item is the most desirable, the optimal strategy is to engineer the 10-fail pity to always hit the floor.
**Fix:** Replace line 558 with `chosen = random.choice([p for p in pool if _rarity_val(p.get("rarity", "common")) >= _rarity_val(guaranteed_rarity)])` — a uniform random pick from the qualifying tier.

---

### WARNING #8: Naked `except Exception:` swallows in `_purchase_item`, `_activate_item`, `_open_lootbox`

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:348-350, 476-478, 647-649`
**Confidence:** 0.85
**Risk:** Each of the three engine functions wraps the atomic-transaction body in `try: ... except Exception: db.rollback(); raise`. The rollback-then-raise is correct, BUT there is no `log.exception()` between catch and re-raise. If the exception bubbles up to a Phase 2 view that itself has `except Exception: pass` (which CLAUDE.md explicitly prohibits in admin views), the failure is invisible. Even if the caller logs, the *site* of the failure (which SQL statement, which transaction phase) is lost because the bare re-raise discards stack context that loggers would otherwise capture at this layer.
**Vulnerability:** Re-raising without logging at the lowest catch point makes Phase 2 debugging dependent on every caller logging well — which CLAUDE.md says is not happening.
**Impact:** Future Phase 2 silent failures will be invisible. This violates the CLAUDE.md rule "Silent `except Exception: pass` in admin-facing views is prohibited. Always `log.exception(...)`." — the file isn't an admin view yet, but it will be wrapped in one.
**Fix:** Add `log.exception("[Store] purchase txn failed for uid=%s item=%s", discord_id, item_id)` (or equivalent) before each `raise`. The cost is tiny and the diagnostic value is large.

---

### OBSERVATION #1: Column types for `purchased_at`/`activated_at`/`expires_at` are TIMESTAMP, but stored values are TEXT strings

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:131, 134-135, 155-156`
**Confidence:** 0.80
**Risk:** SQLite type affinity is loose, but the schema declares `TIMESTAMP` columns and then writes `'%Y-%m-%d %H:%M:%S'`-formatted strings. Comparison operators (`>`, `<`) work on lexicographic order for the standard format, which happens to match chronological order — but only for ASCII-7 strings of the same length. Mixing `_utcnow()` (UTC string) with `CURRENT_TIMESTAMP` (which the SQLite docs say is UTC) WILL generally compare correctly, but it's fragile.
**Impact:** Future date-arithmetic queries that try to use SQL `DATE()` or `DATETIME()` functions may return unexpected results. Migration to a strict-typed DB (Postgres) would break.
**Fix:** Either commit to TEXT columns explicitly, or convert to strict ISO8601 with explicit Z suffix. Document the choice in a comment.

---

### OBSERVATION #2: `_RARITY_RANK` is a hardcoded 4-tier ladder, no extension story

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:36`
**Confidence:** 0.70
**Risk:** Adding a fifth tier ("mythic") requires editing source. Items with `rarity="mythic"` will silently `_rarity_val()` to 0, treated as common, and slip past `guaranteed_rarity` floors.
**Impact:** Future expansion drops items into the wrong tier silently.
**Fix:** Move to a constants module imported by both `flow_store` and `store_effects`. Or add a `KeyError` raise with a clear log message if `_rarity_val()` is called with an unknown rarity.

---

### OBSERVATION #3: `_init_store_db` declares schema but never validates existing columns match

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:79-184`
**Confidence:** 0.80
**Risk:** Standard `CREATE TABLE IF NOT EXISTS` does NOT alter columns if the schema has drifted. If the dev-DB has an older `store_items` schema (e.g., missing `image_url` column added later), the production schema is silently the OLD one, and `dict(item_row)` will still succeed because the column simply isn't in the result set — but writes that include `image_url` will fail at runtime.
**Vulnerability:** Migrations are implicit. There's no version check, no `PRAGMA table_info` reconciliation. The CLAUDE.md mention of `db_migration_snapshots.py` confirms there's no first-class migration framework here.
**Impact:** Schema drift across dev/prod environments. Hard-to-debug "column doesn't exist" errors after deploys.
**Fix:** Add a one-time `PRAGMA table_info(store_items)` check in `_init_store_db()`. If expected columns are missing, log a WARNING (or run an explicit `ALTER TABLE ADD COLUMN`).

---

### OBSERVATION #4: `_expiration_task` runs every 5 minutes — no startup catch-up

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:663-681`
**Confidence:** 0.75
**Risk:** If the bot is offline for 30 minutes, effects that should have expired are still active until the next 5-minute tick. The first tick happens 5 minutes AFTER `wait_until_ready()`, not immediately at startup.
**Impact:** Players retain expired boosts for up to 5 minutes after a restart. Minor balance issue.
**Fix:** In `_before_expiration()`, after `wait_until_ready()`, kick off an immediate `expire_stale_effects()` call before returning, so the loop's first wait gates the *second* run, not the first.

---

### OBSERVATION #5: `_activate_item()` permanent effects never enter the expiration task's purview

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:423-430`
**Confidence:** 0.65
**Risk:** When `duration_hours` is missing or falsy, `expires_at = None`. The expiration task's underlying SQL (`store_effects.expire_stale_effects()` line 244) explicitly excludes `expires_at IS NULL`, so permanent effects live forever. This is correct by design — but there's no kill-switch for admins to revoke a permanent effect short of raw SQL. No `_revoke_effect()` engine method exists.
**Impact:** Phase 2 admin tooling will need a path to revoke permanent badges/trophies. Without it, mistakes are unfixable through normal channels.
**Fix:** Add `async def _revoke_effect(self, effect_id: int) -> None` to set `is_active=0` and log a transaction row.

---

### OBSERVATION #6: Phase 1 promise vs reality — file CONTAINS UI infrastructure (`_get_store_message_id`, `_set_store_message_id`)

**Location:** `C:/Users/natew/Desktop/discord_bot/flow_store.py:683-707`
**Confidence:** 0.95
**Risk:** The file's docstring (lines 1-9) and CLAUDE.md both promise "Phase 1 is headless — no UI, no slash commands, no card rendering." But lines 683-707 declare `# PERSISTENT VIEW STUBS (Phase 2)` and define two helpers that read/write the `sportsbook_settings` table — clearly UI plumbing. These helpers aren't called by anything in Phase 1, so they're inert, but they violate the "Phase 1 contains zero UI surface" promise by including the *stub*. They also depend on `sportsbook_settings` table existing — which is created by `flow_wallet.setup_wallet_db()`, but `flow_store` makes no such guarantee in `_init_store_db()`.
**Vulnerability:** The Phase 2 stubs would fail with `OperationalError: no such table: sportsbook_settings` if `flow_wallet.setup_wallet_db()` hadn't already run by the time they're called. There's an implicit ordering dependency that isn't documented.
**Impact:** Audit-confusing. Future audits will flag the Phase 2 stubs as Phase 1 surface area, and a Phase 2 wiring mistake will hit a missing-table error instead of a clear "table not yet initialized."
**Fix:** Either (a) move the stubs out of `flow_store.py` until Phase 2 lands, or (b) add `# Phase 2 stub — depends on flow_wallet.setup_wallet_db() running first` and a runtime guard `if table not exists: log.warning(...) return None`.

---

## Cross-cutting Notes

- **Phase 1 / Phase 2 boundary is leaky.** The file presents itself as headless but contains Phase 2 UI plumbing stubs. Future audits should verify the same boundary isn't smudged in `flow_sportsbook.py` and the casino subsystem.
- **The `flow_wallet.debit/credit` `reference_key` discipline is broken at the SOURCE in flow_store.py.** Other Ring 1 files (flow_sportsbook, casino, real_sportsbook_cog, prediction_market) likely have similar `uuid.uuid4()`-inside-the-function bugs. Worth grep-checking ALL `reference_key=` call sites for `uuid.uuid4()` or `time.time()` evaluated at call time vs. injected as a parameter.
- **`store_effects.activate_effect()` is the documented public API for inserting effect rows, but the cog bypasses it.** Same pattern likely exists between casino code and `flow_wallet` — code paths that re-implement enforcement instead of calling the canonical helper. Worth a sweep.
- **No log.exception() at the lowest catch points** is a pattern; it likely repeats across all Flow files. The CLAUDE.md "log.exception always" rule is being applied at the view layer, not the engine layer, but engines are usually where the most diagnostic context exists.
- **Schema-drift safety (`CREATE TABLE IF NOT EXISTS` without column reconciliation)** is a project-wide pattern in cogs that own their own tables. A central migration registry would catch all of them at once.

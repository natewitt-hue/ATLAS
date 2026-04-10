# Adversarial Review: casino/casino_db.py

**Verdict:** block
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 1665
**Reviewer:** Claude (delegated subagent)
**Total findings:** 22 (5 critical, 9 warnings, 8 observations)

## Summary

`casino_db.py` is the casino economy primitive layer and carries several load-bearing idempotency and atomicity holes that will corrupt the flow_economy ledger under realistic failure modes. `refund_wager()` has no `reference_key` (confirmed — Ring 1 flag was correct), multiple credit/debit paths cross the async transaction boundary without joining the caller connection, and several "idempotent" paths reuse a single `ref_key` where the same ref would collide across two genuinely distinct operations. Block until the credit/debit hygiene is fixed.

## Findings

### CRITICAL #1: `refund_wager()` omits `reference_key` — Ring 1 flag confirmed

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1089-1100`
**Confidence:** 0.98
**Risk:** Double-refund. Every `flow_wallet.credit()` in the codebase MUST pass `reference_key` per CLAUDE.md Flow Economy Gotchas. This function is called by `casino.py:590` during PvP crash/coinflip abandonments (e.g., `db.refund_wager(user.id, session.wager)` with no correlation). A Discord interaction retry on the caller (e.g., button double-click, 3s timeout retry, failed ack) will result in the credit being applied N times.
**Vulnerability:** The function accepts `correlation_id` but passes it as `subsystem_id` — not as `reference_key`. `subsystem_id` is purely a labeling/linkage field; `flow_wallet.credit()` only deduplicates when `reference_key` is non-null. There is no idempotency guard here at all. Additionally, the `settle_wager` call on line 1099 silently no-ops if the correlation_id already reached terminal status, but the credit above it does not.
**Impact:** Players who get a refund on a declined PvP challenge or voided crash round can receive the refund multiple times by forcing a retry. Pure financial exploit — direct ledger corruption.
**Fix:**
```python
async def refund_wager(discord_id: int, amount: int,
                       correlation_id: str | None = None) -> int:
    ref_key = f"CASINO_REFUND_{correlation_id or discord_id}_{int(time.time())}"
    new_bal = await flow_wallet.credit(
        discord_id, amount, "CASINO",
        description="casino refund",
        reference_key=ref_key,
        subsystem="CASINO", subsystem_id=correlation_id,
    )
    ...
```
But the timestamp-suffix approach is itself not retry-safe; the caller should pass a stable `correlation_id` and this function should derive the ref as `f"CASINO_REFUND_{correlation_id}"`. Update the one caller in `casino.py:590` to pass the session's correlation id.

---

### CRITICAL #2: `deduct_wager()` debit has no `reference_key`

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1046-1086`
**Confidence:** 0.95
**Risk:** Double-debit at bet placement. This is the hot path invoked by every single casino bet (blackjack, slots, crash, coinflip). `flow_wallet.debit()` is called without a `reference_key` argument (line 1069-1075 — it passes `subsystem`, `description`, `subsystem_id`, `con` but NOT `reference_key`).
**Vulnerability:** Explicit violation of the CLAUDE.md Flow Economy Gotcha rule. `subsystem_id=correlation_id` gives you labeling only; the idempotency check inside `flow_wallet.debit()` guards on `reference_key`. A Discord interaction 3s timeout followed by a retry of the bet-placement button will debit twice — player loses double their stake, only one `casino_sessions` row will be created, and the orphan reconciler (lines 278-345) will refund exactly one of the debits while the player already lost twice for one game.
**Vulnerability (part 2):** The user lock on line 1056 (`flow_wallet.get_user_lock(discord_id)`) is process-local. Inside a single Python process it serializes; across bot restarts or a retry that reads the lock after the failed attempt released it, no guard exists.
**Impact:** Every casino game has an exploitable double-debit on retry/crash. Race condition has real probability — buttons are clicked multiple times constantly and Discord's 3s interaction timeout is notoriously tight.
**Fix:** Pass `reference_key=f"CASINO_DEBIT_{correlation_id}"` (require correlation_id to be non-None, generated at the view layer before first interaction ack). The reconciler in `reconcile_orphaned_wagers` will then actually have a hope of telling distinct debits apart.

---

### CRITICAL #3: `reconcile_orphaned_wagers` matches orphans on `(discord_id, abs(amount))` — false-positive refund

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:278-345`
**Confidence:** 0.92
**Risk:** Spurious refunds. The orphan detection SQL (lines 295-316) looks for casino wager debits with `NOT EXISTS (... s.discord_id = t.discord_id AND s.wager = ABS(t.amount) AND s.played_at >= t.created_at)`. If a user places two bets of the same amount within a short window, and the first one crashes mid-game (so no session row exists), but the second one completes successfully after the cutoff, the reconciler will find the orphaned first debit AND ALSO find a matching session (from the second bet). Worse: if the second bet's session hasn't been logged yet at reconcile-time but the first bet's debit is older than cutoff, the query uses `NOT EXISTS` — but `played_at >= t.created_at` can match the second bet's session to the FIRST bet's debit, so the real orphan goes un-refunded.
**Vulnerability:** The matching heuristic uses amount as a synthetic key. Two bets of identical amount + different game_types + overlapping timestamps are indistinguishable from one orphaned bet + one legit session. The idempotency guard `reference_key = 'ORPHAN_' || t.txn_id` does protect against double-refund of the SAME orphan, but does NOT protect against misattribution across distinct bets.
**Impact:** Players can (a) receive refunds they shouldn't have, or (b) real orphans get "absorbed" by later legitimate bets and lose their money permanently. Either way = financial ledger inconsistency, reported player complaints, inability to audit.
**Fix:** Require that the debit row carry a `subsystem_id` (correlation_id) and match orphans by `NOT EXISTS (SELECT 1 FROM casino_sessions s WHERE s.correlation_id = t.subsystem_id)` — unique composite key, no amount collision.

---

### CRITICAL #4: `process_wager` crosses async boundary after `BEGIN IMMEDIATE` without `to_thread()` serialization guard

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:880-1033`
**Confidence:** 0.85
**Risk:** Deadlock / held-lock starvation. `BEGIN IMMEDIATE` is issued on line 881, and the function holds the db write lock across multiple `await flow_wallet.credit()` / `await flow_wallet.get_balance()` calls (lines 936-945, 950, 962, 977, 1027) as well as `await _update_streak()` and `await _contribute_and_check_jackpot()`. Each of these can suspend for unknown time (the jackpot check fetches the `casino_jackpot_boost` setting on line 489, credit calls may block on user locks inside flow_wallet, etc.). While suspended, NO OTHER casino operation anywhere in the bot can begin a write transaction — `BEGIN IMMEDIATE` holds a RESERVED lock until commit/rollback.
**Vulnerability:** If any inner await blocks for > a few hundred ms (e.g., flow_wallet user lock contention from a parallel `deduct_wager` for the same user, since `deduct_wager` also takes `BEGIN IMMEDIATE`), you get a lock chain: process_wager holds SQLite RESERVED lock → deduct_wager waits for SQLite RESERVED → inner credit inside process_wager wants flow_wallet user lock held by deduct_wager → deadlock. Additionally, any exception from a nested call leaves the transaction suspended; the `except Exception: rollback` on line 1031-1033 only catches synchronous exceptions (which is fine for `await raise`, but the rollback itself can fail silently on a detached connection after a crash).
**Impact:** Under concurrent play (the stated use case: "multiple users gamble at once" — line 5), the casino can deadlock or wedge its write lock for extended periods. Silent bet losses and timeouts for waiting players.
**Fix:** Pre-compute all derived values (streak bonus, cold mercy, jackpot roll) before `BEGIN IMMEDIATE`, then enter the critical section and perform only the minimal atomic writes. Or, split into a read-phase and a write-phase with `BEGIN` deferred until the actual row inserts.

---

### CRITICAL #5: `process_wager` streak bonus ref_key can collide on same-streak concurrent calls

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:948-957`
**Confidence:** 0.90
**Risk:** Silent drop of legitimate second bonus. `streak_bonus_ref = f"streak_bonus_{discord_id}_{game_type}_{streak_info['len']}"` — this ref is idempotency-keyed only on (user, game, streak_length). A user at streak length 5 who wins two games of the SAME game_type back-to-back will compute the same streak bonus ref on both wins (len=5 both times if this is called in parallel, or len=5 then len=6). Wait — if it's len 5 then len 6, refs differ. BUT if two concurrent winning bets both complete with streak_len=5 (because `_update_streak` runs inside a transaction but was pre-computed separately), both calls compute `streak_bonus_<uid>_<game>_5` — flow_wallet will idempotently swallow the second, and the player loses a legitimate bonus.
**Vulnerability:** The idempotency key needs to include something monotonically unique per bet (e.g., `session_id`), not the derived streak length. The rest of the function does include `session_id`, which proves the author knew this — but this line slipped through.
**Impact:** Every casino player on a hot streak has a non-zero chance of losing a streak bonus to idempotency collision. The collision rate scales with concurrent play.
**Fix:** `streak_bonus_ref = f"streak_bonus_{session_id}"` — session_id is per-bet and already in scope via `sid`.

---

### WARNING #1: `setup_casino_db` silently swallows `ALTER TABLE` errors — hides real schema drift

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:158-165, 220-231, 233-239`
**Confidence:** 0.80
**Risk:** Schema migrations fail silently. Every `ALTER TABLE ... ADD COLUMN` is wrapped in `try/except Exception: pass  # column already exists`. This works for the "column exists" case but will ALSO silently swallow other errors — database locked, disk full, permission denied, typo in column definition. On older DBs where the column isn't there but the ALTER errors out for a real reason, downstream code queries fields that don't exist.
**Vulnerability:** SQLite's `ALTER TABLE` returns an error like `duplicate column name: X`. Catching ALL `Exception` instead of narrowing to this specific case is a lazy shortcut that hides schema corruption. The comment `# column already exists` is aspirational, not factual.
**Impact:** If a migration partially succeeds (e.g., adds `login_streak` but fails on `last_streak_date`), subsequent queries referencing `last_streak_date` throw `OperationalError` at runtime. No observability into the root cause because the exception was swallowed at startup.
**Fix:** Narrow the except: `except aiosqlite.OperationalError as e: if "duplicate column name" not in str(e): log.error(...); raise`.

---

### WARNING #2: `backfill_jackpot_tags` matches credits by (discord_id, amount) — ambiguous join

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:579-603`
**Confidence:** 0.85
**Risk:** Wrong subsystem_id tagged on historical rows. The migration correlated subquery joins `casino_jackpot_log` on `jl.discord_id = transactions.discord_id AND jl.amount = transactions.amount ORDER BY jl.won_at DESC LIMIT 1`. If a user won multiple jackpots of the same tier (say, two mini jackpots of $100 each at different times), ALL matching transaction rows get tagged with the SAME log_id (the most recent by `won_at DESC`).
**Vulnerability:** This is called from bot.py `setup_hook` on every startup (per the file header comment). It's gated by `WHERE subsystem IS NULL`, so it's idempotent in terms of "will not re-tag an already-tagged row" — but the very first run on historical data is LOSSY and can't be retried. Wrong attribution is baked in.
**Impact:** Audit trail misattribution. Subsystem_id for half the historical jackpot credits points at the wrong log row. Not a financial bug, but blocks any future reconciliation / fraud investigation that uses this field.
**Fix:** Match on transaction time proximity: `ORDER BY ABS(julianday(jl.won_at) - julianday(transactions.created_at)) ASC LIMIT 1`. Or, accept the backfill is best-effort and document it — but at minimum don't silently run on every startup (guard with a flag in `sportsbook_settings` so it runs once).

---

### WARNING #3: `_contribute_and_check_jackpot` awards jackpot inside write txn but does not check if pool is "seed-only"

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:513-566`
**Confidence:** 0.80
**Risk:** Players can "win" the seed amount repeatedly. `_award_jackpot` reads `pool, seed` from `casino_jackpot`, pays out `amount = row[0]` (current pool), then `UPDATE casino_jackpot SET pool = ?` using `seed_val`. If the pool has never been contributed to (players haven't wagered much yet), the pool equals the seed and a jackpot roll awards the seed to the player, resets to the same seed. Then another roll on the very next bet can award the seed again — no cooldown, no "must accumulate" gate.
**Vulnerability:** The sanity check `if amount < 1: return None` only guards against zero pools. A pool reset to seed=100 and immediately re-rolled will pay out 100 again. No "minimum pool above seed" guard.
**Impact:** Jackpot exploit — whoever plays shortly after a jackpot win can re-trigger at cheap house cost. House bank leaks seed_val * N on every roll in quick succession.
**Fix:** Require `pool > seed * 1.1` (or similar) before awarding, OR mark the pool with a `last_won_at` cooldown (already stored but unused in gate logic).

---

### WARNING #4: Blocking `date.today().isoformat()` and `datetime.now()` called in hot sync helpers — timezone confusion

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:612, 629`
**Confidence:** 0.70
**Risk:** Streak calculation uses `date.today()` (local timezone) while `claim_scratch` uses `datetime.now(timezone.utc).date()` (UTC). Two different timezone conventions for the same logical "day" across the same file.
**Vulnerability:** Players whose local-day boundary differs from the bot host's day boundary will see streak mechanics behave differently from scratch-claim mechanics. Specifically: `get_streak` returns `row[3] != today` as the "streak broken" signal where `today = date.today()`, but the streak was stored on an earlier day using `date.today()`. If the bot host is in UTC and the UI (or the player) thinks in local time, midnight rollover becomes inconsistent.
**Impact:** Confusion, support tickets. Non-financial but observable.
**Fix:** Standardize on `datetime.now(timezone.utc).date().isoformat()` everywhere. Lines 612 and 629 both need updating.

---

### WARNING #5: `resolve_crash_round` returns tuple by index without checking row schema

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1443-1475`
**Confidence:** 0.75
**Risk:** Brittle positional field access. The function does `SELECT * FROM crash_bets` then indexes rows by position: `r[0] = id, r[1] = round_id, ..., r[6] = status`. The `crash_bets` schema (lines 121-131) has 7 columns: `id, round_id, discord_id, wager, cashout_mult, payout, status`. Any future ALTER TABLE that adds a column before `status` (or a migration that reorders columns) silently corrupts the dict mapping.
**Vulnerability:** No `db.row_factory = aiosqlite.Row` set, unlike `get_crash_round` (line 1352). Inconsistent pattern in the same file.
**Impact:** Future migration hazard. Not an immediate bug.
**Fix:** Use `db.row_factory = aiosqlite.Row` and index by name: `r["id"]`, `r["status"]`.

---

### WARNING #6: `void_stale_crash_bets` — credit loop holds db connection but no `BEGIN IMMEDIATE`

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1400-1440`
**Confidence:** 0.80
**Risk:** Non-atomic refund loop. The function opens a connection, queries stale bets, then iterates through the `stale` list and for each one: (1) marks bet voided, (2) calls `flow_wallet.credit()` WITHOUT passing `con=db`, (3) appends to result. No `BEGIN IMMEDIATE`, no transaction boundary. If the bot crashes midway through the loop, some bets are marked `voided` without their refund being applied (or vice versa).
**Vulnerability:** `flow_wallet.credit()` with `con=None` opens its own connection and commits — so the refund IS atomic as far as flow_economy goes, but the `UPDATE crash_bets SET status='voided'` lives on the outer connection and is NOT committed until line 1438 at the end of the loop. If the loop errors midway, NONE of the voided status updates commit, but SOME of the refunds have already been credited. Next call to `void_stale_crash_bets` will find the same stale bets, refund them again. Idempotency is guarded by `reference_key=f"CRASH_TIMEOUT_{bet_id}"` on line 1427 — so duplicate refunds are blocked. OK on that axis.
**Vulnerability (the real bug):** The `reference_key` uses `bet_id` alone — if a bet is manually re-voided (a commissioner runs this twice after fixing a bug), the second refund is correctly suppressed. BUT the status update on the bet row succeeded the second time, which is fine. So the idempotency guard here holds.
**Revised Risk:** Weaker than I initially assessed. The remaining concern is the status-update / refund ordering: if refund succeeds but status update fails on commit, the bet stays `active` forever and blocks new rounds. Observability gap.
**Impact:** Low-probability, operator-recovery scenario.
**Fix:** Wrap the loop body in `BEGIN IMMEDIATE` and pass `con=db` to `flow_wallet.credit()`.

---

### WARNING #7: `process_wager` reopens `wager_registry` import inside txn — module import lock hazard

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:995, 1003, 328`
**Confidence:** 0.60
**Risk:** Import inside transaction. `import wager_registry` is placed inline inside transactional code on lines 995, 1003, and also in `reconcile_orphaned_wagers` line 328 and `claim_scratch` line 1280. Python imports hold a module lock; while the first-time import is running, any other thread trying to import the same module blocks. In async code this manifests as a suspended task.
**Vulnerability:** Inside `BEGIN IMMEDIATE` transaction, if two concurrent `process_wager` calls both first-import `wager_registry`, the second blocks on the import lock while holding the SQLite RESERVED lock.
**Impact:** Low probability (once imported, the module is cached), but the pattern is a code smell hiding a genuine bootstrap hazard.
**Fix:** Move `import wager_registry` to the top of the file. It already IS at the top of most callers — there's no circular-import reason to lazy-import it here.

---

### WARNING #8: `get_streak` returns stale `max_streak` for a new-day reset

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:610-621`
**Confidence:** 0.70
**Risk:** Wrong max streak after day rollover. The function does `if not row or row[3] != today: return {"type": "none", "len": 0, "max": row[2] if row else 0, "date": today}`. But `row[2]` is the all-time max — that's correct. The real issue: when `_update_streak` does NOT see a new day (it does — line 644-645 resets prev_type/prev_len for different day), the display read from `get_streak` correctly shows a fresh streak. OK.
**Revised concern:** `get_streak` does NOT update the DB on a day rollover — it reads the old row and returns a "fresh" dict. But if the caller then calls into anything that reads `casino_streaks.streak_len` directly (bypassing `get_streak`), they see the stale old value.
**Impact:** Low; the UI layer consistently uses `get_streak`. Design coupling risk.
**Fix:** Clarify contract or auto-update on read.

---

### WARNING #9: `cashout_crash_bet` returns 0 silently on unknown/inactive bet — no logging

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1372-1397`
**Confidence:** 0.75
**Risk:** Silent swallow in admin/user-facing flow. `if not row or row[1] != "active": return 0` — no warning, no log. A user who legitimately tries to cash out, has a bet_id mismatch (e.g., the round was auto-voided by `void_stale_crash_bets` a second before), gets a zero payout with no explanation. The caller in `casino.py` then sees `payout=0` and may treat it as a loss.
**Vulnerability:** No distinction between "no such bet" vs "already cashed out" vs "round already resolved". All three collapse to `return 0`. This is close to a silent admin-facing `except: pass` — CLAUDE.md explicitly forbids this.
**Impact:** Users complain "it said I cashed out but I got nothing." Support burden.
**Fix:** Raise distinct exceptions (`BetNotFoundError`, `BetAlreadyResolvedError`, `RoundTimedOutError`) and let the caller decide how to present each case.

---

### OBSERVATION #1: Conflict between table name `sportsbook_settings` and casino-specific keys

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:246-269`
**Confidence:** 0.90
**Risk:** Naming confusion. The casino stores its settings in a table literally named `sportsbook_settings` (`CREATE TABLE IF NOT EXISTS sportsbook_settings`). All keys are prefixed `casino_*` but the table is shared with the sportsbook subsystem. Adding a key named, say, `casino_max_bet` when sportsbook has `max_bet` is safe — but the shared namespace is a landmine for future column drift.
**Impact:** Pure maintenance hazard.
**Fix:** Rename to `app_settings` or `global_settings`, or split into `casino_settings` + `sportsbook_settings`. Comment already acknowledges this is shared (line 245) but doesn't fix the table name.

---

### OBSERVATION #2: `CASINO_MAX_PAYOUT = 10_000_000` cap silently truncates winnings

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:32, 917-919`
**Confidence:** 0.85
**Risk:** A legitimate 10,000x crash payout on a $1,100 bet gets silently capped, player sees $10M instead of $11M, no notification. The log line says "capping to" but the user never sees it.
**Impact:** Player will notice the missing $1M and complain. Potentially a trust issue.
**Fix:** Return the capped amount AND a flag in the result dict so the caller can surface it in the UI.

---

### OBSERVATION #3: `_generate_crash_point` uses `secrets.token_bytes(8)` but logs only the hash

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1301-1333`
**Confidence:** 0.80
**Risk:** Provable-fairness claim is weakened. The function returns `seed` (hex of entropy) but logs `seed_hash` via `log.info`. To verify a round post-hoc, a player needs BOTH the hash (committed before the round) and the seed (revealed after). The current flow stores `seed` in `crash_rounds` but never exposes it to players. The "proof" is incomplete.
**Impact:** Marketing claim ("provably fair") unsupported by exposed interface. Trust risk.
**Fix:** Expose the round's seed to `get_crash_round` return dict AFTER the round has crashed, not before.

---

### OBSERVATION #4: `get_jackpot_pools` uses `SELECT *` with positional zip — schema drift hazard

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:451-457`
**Confidence:** 0.70
**Risk:** The function does `SELECT *` and builds a dict by zipping with a hardcoded column list. If anyone adds a column to `casino_jackpot`, the positional mapping breaks silently.
**Impact:** Future migration footgun.
**Fix:** Select explicit columns or use `db.row_factory = aiosqlite.Row`.

---

### OBSERVATION #5: `seed_jackpot` admin function has no idempotency, no audit log

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:569-576`
**Confidence:** 0.75
**Risk:** Commissioner double-seeds a jackpot (runs `/god seed_jackpot 10000` twice by accident) — pool receives 20000, no record. No entry in transactions table, no log line.
**Impact:** Admin action drifts the economy with no audit trail. Blocks post-hoc investigation.
**Fix:** Insert a row into a new `casino_admin_log` or `transactions` with subsystem=CASINO_ADMIN; take a `reference_key` argument for idempotency.

---

### OBSERVATION #6: `ACHIEVEMENTS` dict has 20 entries but `checked_achievements` only covers ~13

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:696-717, 731-836`
**Confidence:** 0.75
**Risk:** Unreachable achievements. `perfect_hand`, `nerves_of_steel`, `dedicated`, `iron_will`, `crowd_player` appear in the `ACHIEVEMENTS` dict but `check_achievements` doesn't implement their unlock logic. Players will see them listed but can never earn them.
**Impact:** UI shows unachievable goals. Confusion / complaints.
**Fix:** Either implement the checks (matching the achievement `desc` field) or gate the dict to only show what's actually implemented.

---

### OBSERVATION #7: Multiple `import wager_registry` statements at module-internal scope

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:328, 549, 995, 1003, 1280, 1537`
**Confidence:** 0.90
**Risk:** Six separate inline imports of the same module. This is a code smell suggesting the author was dodging a circular-import at some point. Circular imports in this codebase are usually indicators that the cog boundary is in the wrong place.
**Impact:** Code clarity and the import-lock risk flagged in Warning #7.
**Fix:** Hoist `import wager_registry` to the top of the file alongside `import flow_wallet`.

---

### OBSERVATION #8: `get_house_report` `REPLACE(LOWER(label), 'casino ', '')` is fragile

**Location:** `C:/Users/natew/Desktop/discord_bot/casino/casino_db.py:1118-1121`
**Confidence:** 0.80
**Risk:** Label parsing by string replace. The query infers `game_type` by stripping `"casino "` from the beginning of the `label` field. Any label change in `register_wager` calls (e.g., someone writes `"Casino Blackjack"` vs `"casino blackjack"` vs `"BLACKJACK"` for a new game) silently drops or miscategorizes rows in the house report.
**Impact:** House P&L reporting drifts as labels change. Commissioner dashboard shows wrong numbers.
**Fix:** Store `game_type` as an explicit column on `wagers`, or join to `casino_sessions` on `subsystem_id = session_id` and pull `game_type` from there.

## Cross-cutting Notes

Three file-wide patterns that likely repeat in other Ring 2 casino files:

1. **`reference_key` omission in wallet calls is endemic.** Both `refund_wager` and `deduct_wager` — the two money-in/money-out primitives of the casino — omit `reference_key` entirely. Suspect similar holes in `casino.py`, `casino/session.py`, any PvP handler, and possibly sportsbook settlement paths. This contradicts CLAUDE.md and should be a blanket sweep, not a per-file fix.

2. **Silent `except Exception: pass` for ALTER TABLE migrations.** Five places in this file. The same "defensive schema migration" pattern likely exists in `flow_wallet.py`, `wager_registry.py`, `sportsbook.py`. Recommend a shared `safe_add_column(db, table, col_def)` helper that narrows the except to `duplicate column name`.

3. **`BEGIN IMMEDIATE` held across multiple awaits.** The long write-critical section in `process_wager` is a pattern. Recommend a policy: all logic that can be pre-computed before the critical section should be, so `BEGIN IMMEDIATE` is held for milliseconds not seconds. Check `sportsbook_core.py` settlement for the same pattern.

# Adversarial Review: flow_sportsbook.py

**Verdict:** block
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 4122
**Reviewer:** Claude (delegated subagent)
**Total findings:** 38 (10 critical, 14 warnings, 14 observations)

## Summary

This is the highest-financial-blast-radius file in the bot, and it contains multiple ledger-corruption hazards in production-active code paths: parlay debits, prop debits, admin balance adjustments, and bet refunds all bypass the `reference_key` idempotency contract that `flow_wallet.update_balance_sync` requires to survive Discord retries. Worse, the entire admin matchup-keyed command surface (`setspread`, `setou`, `lock`, `setml`) writes to a key the read path never queries — admin overrides and locks for the unified-hub `gameId` flow are silently dropped on the floor. The file ships, but every one of these lands as a real incident sooner or later. Block until the critical findings are addressed.

## Findings

### CRITICAL #1: Parlay debit omits `reference_key` — double-spend on Discord retry

**Location:** `flow_sportsbook.py:1353-1356`
**Confidence:** 0.97
**Risk:** A user submitting a parlay can be debited twice for the same wager if Discord retries the modal interaction, the user double-clicks Submit, or the network drops during the 3-second response window.
**Vulnerability:** `flow_wallet.update_balance_sync(... subsystem="PARLAY", subsystem_id=parlay_id ...)` is called WITHOUT a `reference_key` argument. Per CLAUDE.md "Flow Economy Gotchas," every debit/credit MUST pass `reference_key`. The wallet's idempotency check at `flow_wallet.py:543` is bypassed when `reference_key` is None, so each retry produces a fresh `transactions` row and a fresh balance decrement. Even though `parlay_id` is a freshly generated `uuid.uuid4()` that won't repeat, the SAME `parlay_id` is used by both retries — and without a reference_key the wallet will happily debit the user twice for the same parlay_id. The straight bet path at line 1110 uses `f"TSL_BET_DEBIT_{uid}_{game_id}_{int(time.time())}"` correctly; this path was missed in that fix.
**Impact:** Direct silent over-debit. User submits a $1,000 parlay, gets charged $2,000, sees only one parlay in their history. Financial corruption with no audit trail tying the duplicate debit back to the same intent.
**Fix:** Add `reference_key=f"PARLAY_DEBIT_{interaction.user.id}_{parlay_id}"` to the `update_balance_sync` call. The parlay_id is unique per submission attempt and survives across the modal lifetime, so it's the right idempotency anchor.

---

### CRITICAL #2: Prop bet debit omits `reference_key` — double-spend on Discord retry

**Location:** `flow_sportsbook.py:1499-1502`
**Confidence:** 0.97
**Risk:** Same retry-double-spend bug as #1, but on the prop bet placement path.
**Vulnerability:** `flow_wallet.update_balance_sync(... subsystem="PROP", subsystem_id=str(wager_id) ...)` is called without `reference_key`. The `wager_id` comes from `last_insert_rowid()` immediately above (line 1498), so on a Discord retry the INSERT runs again and creates a NEW `wager_id`, making the second `wager_id`-keyed debit a different row. Idempotency completely defeated.
**Impact:** Double-charged prop bets. The duplicate `prop_wagers` row is also a phantom bet — only one of the two will get settled, and the user is silently down 2× the wager amount.
**Fix:** Move the `INSERT INTO prop_wagers` and the debit into a single `reference_key` anchor based on a stable identifier — e.g., `reference_key=f"PROP_DEBIT_{interaction.user.id}_{self.prop_id}_{int(time.time())}"`. Ideally, generate a client-side wager UUID *before* the INSERT and use it as the idempotency key.

---

### CRITICAL #3: Admin bet refund credit omits `reference_key` AND subsystem tags

**Location:** `flow_sportsbook.py:3926`
**Confidence:** 0.95
**Risk:** A commissioner refunding a bet via `/sb refund <bet_id>` can credit the user twice if the command is invoked twice (intentionally or by retry). There is no idempotent guard, no subsystem tag, and no audit linkage to the original bet.
**Vulnerability:** `_update_balance(uid, amt, con)` is called with NO `reference_key`, NO `subsystem`, NO `subsystem_id`. Compare to the cancel-game refund path at line 3832 which passes all three. The `bet_id` is the natural idempotency anchor here and it's available — there is no excuse.
**Impact:** Direct duplicate credit. Forensically untraceable refund (no `subsystem='TSL_BET'` row in the ledger linking to the bet).
**Fix:** `_update_balance(uid, amt, con, subsystem="TSL_BET", subsystem_id=str(bet_id), reference_key=f"TSL_BET_{bet_id}_admin_refund")`.

---

### CRITICAL #4: Admin balance adjustment omits `reference_key` AND subsystem tags

**Location:** `flow_sportsbook.py:3964`
**Confidence:** 0.96
**Risk:** Commissioner balance adjustments via `/sb balance @user +1000` are non-idempotent and untagged. Re-running the command (or a retry) silently double-applies the adjustment.
**Vulnerability:** `_update_balance(member.id, adjustment)` is called with NO connection, NO `reference_key`, NO `subsystem`, NO `subsystem_id`. The subsequent ledger post via `post_transaction` writes to `#ledger` channel, but the underlying wallet transaction has no audit attribution beyond a default `source="TSL_BET"` (which is wrong — this is an ADMIN action, not a TSL bet).
**Impact:** Balance corruption with mislabeled audit trail. An admin issuing "+$5,000 for losing the season finale" twice silently grants $10,000 with the bet transaction source.
**Fix:** `_update_balance(member.id, adjustment, subsystem="ADMIN", subsystem_id=f"adj_{member.id}_{int(time.time())}", reference_key=f"ADMIN_ADJ_{interaction.user.id}_{member.id}_{int(time.time())}")` AND switch the source kwarg in `_update_balance` (or call `flow_wallet.update_balance_sync` directly) to `source="ADMIN"`.

---

### CRITICAL #5: Admin matchup-string commands write to a key the read path never queries

**Location:** `flow_sportsbook.py:3695-3744` (`_sb_setspread_impl`, `_sb_setml_impl`, `_sb_setou_impl`), `flow_sportsbook.py:3760-3766` (`_sb_lock_impl`)
**Confidence:** 0.94
**Risk:** Every admin override / lock command keyed by user-typed `matchup` string is silently a no-op for the actual betting flow. Commissioners think they've adjusted lines or locked a game; users still see the engine values and can still bet.
**Vulnerability:** These commands call `_set_line_override(matchup.strip(), ...)` and `_set_locked(matchup.strip(), locked)`. Inside `_set_line_override` and `_set_locked`, that string is bound directly to `games_state.game_id` / `line_overrides.game_id`. But `_build_game_lines` at line 845 builds `game_id` as `str(rg.get("gameId", rg.get("id", rg.get("matchup_key", f"{away}@{home}"))))` — preferring the API integer ID. The lookups in `_get_line_override` at line 877 and `_is_locked` at line 1097, 1574, 1856, 1886, 2388 all use this gameId. Therefore an admin who types `Cowboys @ Eagles` writes a row keyed `"Cowboys @ Eagles"` that nothing reads — the actual override is `gameId="123456"`. The unified hub flow (`SportsbookWorkspace`, `SportsbookSelectView`) is the user-facing path, which means the admin override commands as written are dead.
**Impact:** Critical commissioner workflow broken. Spread overrides, ML overrides, O/U overrides, and per-game locks all silently fail. The success message back to the admin lies. Shop will believe the edit took effect.
**Fix:** Resolve the matchup string to the canonical `game_id` first via `_load_tsl_week_games()` (or equivalent), then write using that resolved ID. The `_sb_lockall_impl` at line 3791 already does this correctly (`g.get("gameId", ...)`); copy the resolution pattern. Add a defensive check that errors out if no matching game is found instead of silently writing to a dead key.

---

### CRITICAL #6: Parlay mirror failure leaves user debited but unsettleable

**Location:** `flow_sportsbook.py:1373-1421`
**Confidence:** 0.92
**Risk:** When the mirror to `sportsbook_core` (flow.db) fails, the user's funds are already debited from `flow_economy.db`, the `parlays_table` row is committed locally, the cart is cleared, and the user sees a SUCCESS confirmation card — but the parlay does not exist in the system that runs settlement.
**Vulnerability:** Lines 1330-1364 commit the local debit + parlays_table + parlay_legs rows in flow_economy.db. Lines 1376-1421 then attempt to mirror to flow.db. On exception, the cleanup at lines 1412-1419 only deletes the partial flow.db rows — it does NOT roll back the local debit, does NOT roll back the local `parlays_table` row, and does NOT refund the user. Execution then falls through to line 1423-1448 which renders the SUCCESS card and clears the cart. Settlement runs against flow.db (`sportsbook_core.settle_event`), so a parlay that exists only in flow_economy.db will never settle.
**Impact:** Silent permanent loss of funds. The local `parlays_table` row marked `Pending` will sit forever unless an admin notices and force-settles it. Users will see "Parlay placed!" then never see a payout. Refund/cancel commands work on `parlays_table`, so manual recovery is possible — but the user has no way to know it's needed.
**Fix:** On mirror failure, refund the local debit (via `flow_wallet.update_balance_sync` with a unique reference_key), update `parlays_table` status to `Cancelled`, and surface an error to the user instead of the success card. Better: write to flow.db first inside the same async transaction, only commit local state after both succeed (two-phase commit pattern).

---

### CRITICAL #7: Silent except in admin-facing _open_bets_impl swallows ESPN bet query failures

**Location:** `flow_sportsbook.py:3467-3478`
**Confidence:** 0.95
**Risk:** Per CLAUDE.md "Flow Economy Gotchas," silent `except Exception: pass` in admin-facing views is PROHIBITED. Admins viewing the Open Bets browser get an incomplete list (ESPN bets entirely missing) with NO indication that a query failed.
**Vulnerability:** The try block wraps the entire ESPN query. ANY exception — table doesn't exist, schema mismatch, lock contention, OperationalError, network glitch — leaves `espn_bets = []` with zero log entry. An admin investigating a payout dispute will see only TSL bets and conclude that no ESPN bets exist for that user.
**Impact:** Silent data hiding in the commissioner audit surface. Worst case: an admin force-settles based on an incomplete view, paying out to a user who actually has zero open bets at all (they were all hidden).
**Fix:** Replace `except Exception: pass` with `except Exception: log.exception("[SB] _open_bets_impl: ESPN query failed")` AND surface a warning in the embed footer or a status field so the admin knows the data is incomplete.

---

### CRITICAL #8: Silent except in admin-facing _settled_bets_impl swallows ESPN bet query failures

**Location:** `flow_sportsbook.py:3525-3537`
**Confidence:** 0.95
**Risk:** Identical pattern to #7 — admin Settled Bets browser silently hides ESPN bet query failures.
**Vulnerability:** Same try/except/pass structure on the ESPN query. Same prohibition violation per CLAUDE.md.
**Impact:** Admin reviewing settled bet history gets a TSL-only view that looks complete. Past force-settle errors, refund disputes, and accounting reconciliation all become unreliable.
**Fix:** Same as #7 — `log.exception(...)` plus a footer warning.

---

### CRITICAL #9: TOCTOU race in `_sb_refund_impl` between status check and refund

**Location:** `flow_sportsbook.py:3909-3931`
**Confidence:** 0.85
**Risk:** Two commissioners (or one commissioner double-clicking) can refund the same pending bet twice. The status check at line 3921 happens BEFORE the credit at line 3926, with no row-level lock; sqlite's default isolation does not protect this read-then-write pattern.
**Vulnerability:** Inside the `with _db_con() as con:` block there is no `BEGIN IMMEDIATE` and no `WHERE status='Pending'` guard on the UPDATE. The structure is:
1. Read status (line 3911-3915)
2. Validate (line 3921-3925)
3. Credit (line 3926)
4. UPDATE status (line 3927-3929)

A second concurrent invocation can pass the validation before the first one commits the UPDATE, then both proceed to credit the user twice. The connection's implicit transaction does not lock the row for other writers.
**Impact:** Double refund on concurrent admin action. Combined with #3 (no reference_key) the wallet has no defense against this either.
**Fix:** `con.execute("BEGIN IMMEDIATE")` at the top, then make the UPDATE conditional: `UPDATE bets_table SET status='Cancelled' WHERE bet_id=? AND status='Pending'` and check `con.total_changes` to confirm exactly one row was updated. Combine with reference_key fix from #3.

---

### CRITICAL #10: Lockall does not actually use real game_id, falls back to matchup_key

**Location:** `flow_sportsbook.py:3791-3795`
**Confidence:** 0.85
**Risk:** When `g.get("gameId")` returns None or empty (which happens in some API states), lockall locks rows keyed `matchup_key` like `"Cowboys @ Eagles"` — same dead-key bug as #5. The bet flow uses gameId, never matchup_key, so the lock has no effect.
**Vulnerability:** Line 3792: `game_id = str(g.get("gameId", g.get("id", g.get("matchup_key", ""))))`. The fallback chain ends at `matchup_key`, which is the human-readable key. `_build_game_lines` line 845 has the SAME fallback chain, so when both fall back, they DO match — but when `gameId` is missing in one read and present in another, the keys diverge and locks are silently broken.
**Impact:** Locks intermittently fail on games where the API briefly omits gameId (which has been observed in MaddenStats edge cases). Users can place bets on games the admin thought were locked.
**Fix:** Force a single source of truth: use `_build_game_lines()`-style resolution to compute the canonical game_id once, then use ONLY that for both lock writes and lookup. Reject games where no stable ID can be resolved.

---

### WARNING #1: Real bet odds re-fetch silently falls back to stale odds on DB error

**Location:** `flow_sportsbook.py:2434-2467`
**Confidence:** 0.85
**Risk:** When the real_events refresh query fails for any reason (lock contention, transient OperationalError, table missing), the code silently falls back to `bet["odds"]` — the stale snapshot from when the user opened the match detail card. The user could be betting at a price that has shifted significantly.
**Vulnerability:** The `except Exception: pass # fall back to cached odds on DB error` at line 2466-2467 is a bare swallow with no log call. If the DB is in a degraded state, every bet placed during that window writes at potentially-wrong odds with no operator visibility.
**Impact:** Sharp users could exploit a degraded DB state. Operationally, the silent fallback masks failures that should trigger alerts.
**Fix:** `except Exception: log.exception("[SB] Real odds refresh failed — using cached odds")`. Consider failing the bet entirely on DB errors instead of using stale odds.

---

### WARNING #2: `backfill_parlay_legs_sync` is not idempotent across interrupted runs

**Location:** `flow_sportsbook.py:3051-3087`
**Confidence:** 0.7
**Risk:** The backfill is called from `bot.py setup_hook` per the audit prompt context. If the process is killed between `con.execute(INSERT OR IGNORE)` and the next `con.commit()` (every 100 parlays), the work is lost — the next run re-checks `existing` for that parlay_id and re-runs the inserts, but the legs that were inserted-but-not-committed are also redone.
**Vulnerability:** The structure batches commits every 100 parlays (line 3084-3085). The `INSERT OR IGNORE` is keyed on `(parlay_id, leg_index)` UNIQUE, so re-runs are safe FOR EACH ROW. However, the count returned reports inserts attempted, not committed. The bigger risk: an interrupted backfill will work correctly on retry only because of the UNIQUE constraint — but the LEGS column at parlays_table line 191 says `DEPRECATED: no longer written (v4.4.0)`, so live parlays may not have legs JSON at all. Backfill would silently skip them.
**Impact:** If new parlays without `legs` JSON (post v4.4.0 schema) coexist with old parlays missing entries in `parlay_legs`, this backfill leaves the new ones unfixed.
**Fix:** Document that backfill only handles legacy parlays. Add an explicit `con.commit()` after each parlay_id row to make the count meaningful, or use a marker table to track completed parlays so re-runs can resume.

---

### WARNING #3: Parlay leg `line=0` is silently dropped when mirroring to flow.db

**Location:** `flow_sportsbook.py:1389-1390`
**Confidence:** 0.85
**Risk:** A pickem (PK) bet has `line=0`, but `leg_line not in (None, "", 0)` evaluates True for 0, so 0-point spreads are mirrored as `leg_line_val = None` instead of `0.0`. Settlement logic in `sportsbook_core` may treat None as "no line" and fail to grade pickems correctly.
**Vulnerability:** Line 1390 `leg_line_val = float(leg_line) if leg_line not in (None, "", 0) else None`. Comparison `0 in (None, "", 0)` returns True via `==`, so any 0 line falls through. Same Python truthiness trap that `_safe_float` was added to fix elsewhere in the file.
**Impact:** Pickem parlay legs may be ungradeable or mis-graded. Admin must intervene.
**Fix:** Use explicit None/empty check: `leg_line_val = float(leg_line) if leg_line not in (None, "") else None`. Allow 0 to flow through.

---

### WARNING #4: `_compute_elo_ratings` cache not protected against concurrent rebuilds

**Location:** `flow_sportsbook.py:455-591`
**Confidence:** 0.75
**Risk:** Two `_load_tsl_week_games` calls running in parallel (different users opening hub simultaneously) both run `loop.run_in_executor(None, _build_game_lines, ...)` which calls `_compute_elo_ratings()`. Both can pass the `if _ELO_CACHE: return` check at line 469, both rebuild from scratch, the second overwrites the first. No correctness bug, but wasted work and a brief window where mid-rebuild state could leak via `_OWNER_SCORING_CACHE` reads.
**Vulnerability:** No `threading.Lock` around the cache. The executor is the default ThreadPoolExecutor which runs in worker threads. Module-level dicts are not safe to mutate concurrently.
**Impact:** Wasted CPU on duplicate Elo rebuilds (each one queries the entire history db). In rare interleavings, `_resolve_scoring` could read a partially-populated cache and return defaults instead of real stats.
**Fix:** Wrap the entire function body in a `threading.Lock`. Or use `functools.lru_cache(maxsize=1)` with cache-clear hook — though that requires hashable args.

---

### WARNING #5: `_invalidate_elo_cache` is called from async `_on_data_refresh` without lock

**Location:** `flow_sportsbook.py:449-453`, `flow_sportsbook.py:3127-3128`
**Confidence:** 0.7
**Risk:** Data refresh callback from data_manager fires `_invalidate_elo_cache()` synchronously from the asyncio loop. If a `_compute_elo_ratings` call is mid-rebuild in an executor thread, the cache is wiped from under it. The thread completes its rebuild and re-assigns `_ELO_CACHE = elo` (line 578), restoring stale data — defeating the invalidation.
**Vulnerability:** No coordination between the refresh callback and the rebuild thread. The callback also runs on the main loop, blocking it briefly while it does dict assignments — fine on its own, but combined with rebuild it creates a stale-data window.
**Impact:** Elo ratings could lag by one full data refresh cycle in worst case, leading to slightly stale spreads.
**Fix:** Same lock as #4. Make the invalidation increment a generation counter; rebuild discards its result if the generation has advanced.

---

### WARNING #6: Per-week line override clear function is dead code

**Location:** `flow_sportsbook.py:397-436`
**Confidence:** 0.95
**Risk:** `_clear_line_overrides_for_week` is defined and exhaustively week-resolves game_ids, but is NEVER called anywhere in the codebase. Dead code carries maintenance risk and rots in place.
**Vulnerability:** Confirmed via grep — only the definition exists in flow_sportsbook.py, no call sites. The function imports from `dm` and is otherwise tightly coupled to data_manager state.
**Impact:** Zero runtime impact, but code bloat and false sense of cleanup capability.
**Fix:** Either delete the function or wire it into `_sb_resetlines_impl` (line 3746) which currently does a global wipe.

---

### WARNING #7: `_sb_lock_impl` and other admin set commands have no error handling

**Location:** `flow_sportsbook.py:3760-3766`, `3695-3744`
**Confidence:** 0.8
**Risk:** No try/except, no defer, no validation. If `_set_locked` or `_set_line_override` raises (DB locked, OperationalError), the interaction times out at 3 seconds with a generic Discord error. Admin has no useful feedback.
**Vulnerability:** Admin commands writing to DB with synchronous sqlite calls and no defer. Acceptable for fast operations under no contention; risky under load.
**Impact:** Bad admin UX, potential timeouts during DB lock contention.
**Fix:** Add `await interaction.response.defer(ephemeral=True)` and `try/except` with `log.exception` and a user-visible error.

---

### WARNING #8: `_sb_balance_impl` does not lock the user during read-modify-write

**Location:** `flow_sportsbook.py:3959-3984`
**Confidence:** 0.8
**Risk:** Reads `old_balance` (line 3963), then calls `_update_balance` (line 3964), then reads `new_balance` (line 3965). Three independent calls, no `flow_wallet.get_user_lock(member.id)` wrapper. Concurrent admin actions or wallet writes from other paths can interleave, producing wrong "before"/"after" values displayed to the admin.
**Vulnerability:** TOCTOU on balance display. Wallet itself is row-locked via SQL, but the display values may be stale.
**Impact:** Confusing admin output ("$1000 → $1500 (+$500)" when actual was $1000 → $1200 → $1700).
**Fix:** Use the user lock and read the balance returned by `update_balance_sync` directly.

---

### WARNING #9: Prop wager loss-path uses zero-amount debit just to log description

**Location:** `flow_sportsbook.py:4066-4071`
**Confidence:** 0.8
**Risk:** The "Lost" branch calls `flow_wallet.update_balance_sync(uid, 0, ...)` solely to write a description row. The call uses `reference_key=f"PROP_SETTLE_{wid}"` — same key as the WIN path at line 4059. If a single prop is settled, then admin re-runs the settle (with a different result), the SECOND settle is silently no-op'd by idempotency.
**Vulnerability:** Sharing reference_keys across mutually exclusive outcomes is fragile. It "works" only because settlement is supposedly one-shot, but the file already has known re-settle paths via force-settle and admin tools.
**Impact:** Re-settling a prop with a corrected outcome silently fails to record the corrected ledger entry.
**Fix:** Differentiate by outcome: `f"PROP_SETTLE_{wid}_won"`, `f"PROP_SETTLE_{wid}_lost"`, `f"PROP_SETTLE_{wid}_push"`. Better: don't use a 0-amount debit just to log; use `wager_registry` for the audit trail and skip the wallet call entirely on losses.

---

### WARNING #10: `_sb_settleprop_impl` has no concurrency guard between bet status update and credit

**Location:** `flow_sportsbook.py:4014-4081`
**Confidence:** 0.7
**Risk:** Settles all wagers in a single connection but no `BEGIN IMMEDIATE`. Two concurrent admin settle calls on the same prop would both observe `status='Open'`, both run the loop, both credit users twice.
**Vulnerability:** No locking on prop_id. The status check at line 4029 → status update at line 4078 has a wide window.
**Impact:** Double payout on concurrent settle.
**Fix:** `con.execute("BEGIN IMMEDIATE")` at top of the with block, and conditional UPDATE: `UPDATE prop_bets SET status='Settled', result=? WHERE prop_id=? AND status='Open'`.

---

### WARNING #11: `_force_settle_impl` does not defer interaction

**Location:** `flow_sportsbook.py:3580-3636`
**Confidence:** 0.85
**Risk:** Synchronous DB ops (line 3587-3617) followed by an awaited `post_bet_settlement` (line 3624) and then `interaction.followup.send`. But there is NO `await interaction.response.defer()` anywhere — Discord will timeout the interaction if the DB ops or ledger post take >3 seconds. The followup at line 3631 will fail with InteractionNotResponded if defer was missed.
**Vulnerability:** Caller (boss panel) MUST have already deferred. If a future refactor calls this without deferring, all force-settle commands break with no obvious symptom.
**Impact:** Force-settle silently times out under DB pressure or when called from a non-deferred context.
**Fix:** Add `if not interaction.response.is_done(): await interaction.response.defer(thinking=True, ephemeral=True)` at the top.

---

### WARNING #12: ParlayWagerModal's `on_submit` rolls back on `InsufficientFundsError` but cart is already locked-in

**Location:** `flow_sportsbook.py:1331-1372`
**Confidence:** 0.7
**Risk:** When the debit raises InsufficientFundsError, the `with _db_con()` context manager exits on the exception, which rolls back the implicit transaction. BUT the `BEGIN IMMEDIATE` was a write lock — if the rollback is not perfect, the parlay row could leak. The user is told their cart was preserved (line 1369), which is true (cart writes are in a different DB connection). On a quick retry the flow restarts cleanly.
**Vulnerability:** Subtle correctness issue: if the connection's `__exit__` raises during rollback (e.g., closed connection), the `parlays_table` row from line 1334 could be partially committed.
**Impact:** Phantom pending parlay row with no associated wallet debit. Settlement would attempt to credit a "winner" who never actually paid.
**Fix:** Explicit `con.execute("ROLLBACK")` in the InsufficientFundsError handler, before the followup.send.

---

### WARNING #13: Real bet placement column lookup table mishandles Spread/Over/Under sides

**Location:** `flow_sportsbook.py:2454-2463`
**Confidence:** 0.65
**Risk:** The `_col` mapping uses `(_bt, _pick == _home)` as the lookup key, but for the totals case ("Over"/"Under") the key tuple becomes `("Over", True/False)` based on whether the user's pick string equals the home team name — which it never does for "Over"/"Under" picks. So Over/Under always falls into the `(_, False)` branch, which maps to `over_odds` for Over and `under_odds` for Under. This happens to be correct, but only by accident. If the home team were ever named "Over" or "Under" (impossible in practice but the abstraction is fragile), the table would break.
**Vulnerability:** Brittle lookup contract. The code happens to work because no team is named "Over". A future refactor could break this silently.
**Impact:** Latent bug; minor.
**Fix:** Special-case totals before the lookup: `if _bt in ("Over", "Under"): _col = "over_odds" if _bt == "Over" else "under_odds"`.

---

### WARNING #14: Auto-grade `_autograde_health` global mutated without lock

**Location:** `flow_sportsbook.py:127-136` (declaration), referenced from `_autograde_status_impl` line 3424
**Confidence:** 0.6
**Risk:** Module-level dict mutated by background tasks (presumably) and read by admin command. No lock. Reads of `last_run_at` etc. could see torn state.
**Vulnerability:** Dict mutation in CPython is GIL-protected at the operation level but compound updates (read-then-write) are not atomic. The current code only reads, not mutates, in this file — but the writers are presumably elsewhere. Still, the test at line 3437 (`if last:`) followed by `last.timestamp()` is a TOCTOU if `last_run_at` is concurrently set to None.
**Impact:** Rare race producing AttributeError in admin status output.
**Fix:** Use a `threading.Lock` or hold a snapshot dict copy before reading.

---

### OBSERVATION #1: Magic numbers in Elo computation lack documentation

**Location:** `flow_sportsbook.py:113-122`
**Confidence:** 0.9
**Risk:** ELO_K_NEW, ELO_K_MID, ELO_K_EST, ELO_SEASON_REGRESS, ELO_OWNER_WEIGHT, ELO_TEAM_WEIGHT, SPREAD_SCALING are all magic constants. Comments are present for some but not all. If the league grows or the meta shifts, these need tuning and the rationale is lost.
**Fix:** Add a docstring block explaining derivation and tuning history.

---

### OBSERVATION #2: `ELO_INITIAL` used as both starting rating and unknown-user fallback

**Location:** `flow_sportsbook.py:614-624`
**Confidence:** 0.8
**Risk:** `_resolve_elo` returns `ELO_INITIAL = 1500` for unknown users. This is indistinguishable from a user with exactly 1500 Elo. Downstream consumers can't tell "no data" from "average user".
**Fix:** Track an "Unknown" sentinel separately or expose a `confidence` value.

---

### OBSERVATION #3: `_get_power_map` does no caching

**Location:** `flow_sportsbook.py:642-663`
**Confidence:** 0.75
**Risk:** Iterates `dm.df_power.iterrows()` on every `_build_game_lines` call. For each game in the week, the entire power map is rebuilt — quadratic-ish in worst case. Probably fine for ~16 games but smells.
**Fix:** Cache by `df_power` identity or last-modified hash.

---

### OBSERVATION #4: `_LEAGUE_AVG_SCORE` mutated as module global with no lock

**Location:** `flow_sportsbook.py:445`, `562-570`
**Confidence:** 0.7
**Risk:** Module global mutated inside `_compute_elo_ratings`. Same concurrency issue as the cache. No lock.
**Fix:** Lock or store inside the cache dict.

---

### OBSERVATION #5: `_resolve_scoring` defaults dict shares mutable state

**Location:** `flow_sportsbook.py:629`
**Confidence:** 0.6
**Risk:** Returns a fresh `defaults` dict each call (since it's defined inside the function). No actual bug, but if a future refactor moves `defaults` to module scope, mutations downstream would corrupt the default for all callers.
**Fix:** Document the contract or use `types.MappingProxyType` for immutability.

---

### OBSERVATION #6: `_combine_parlay_odds` returns 100 on empty/zero-only inputs

**Location:** `flow_sportsbook.py:796-809`
**Confidence:** 0.7
**Risk:** `if decimal <= 1.0: return 100  # fallback: even odds if all legs were zero/cancelled`. Returning 100 (even money) on cancelled-only parlays is silent data loss — the caller should be told the parlay is invalid, not given a default.
**Fix:** Raise ValueError or return None and force the caller to handle.

---

### OBSERVATION #7: Hub view rebuilds entire lookup logic on every render

**Location:** `flow_sportsbook.py:2729-2750`
**Confidence:** 0.6
**Risk:** SportsbookHubView `__init__` runs a SQLite query just to update the Cart button label. Acceptable, but that's a synchronous DB call inside Discord's view init path. Multiplied by every `/sportsbook` invocation.
**Fix:** Cache the cart count or update the badge async via followup.

---

### OBSERVATION #8: `_real_short_name` truncates words mid-string in unexpected ways

**Location:** `flow_sportsbook.py:65-76`
**Confidence:** 0.75
**Risk:** "Houston Cougars" becomes "Houston" (12 chars), but "Mississippi State" → "Mississippi" (11 chars, but "Mississippi State" is 17 chars, the candidate "Mississippi State" = 17 > 12 so truncates to "Mississippi"). Edge case: a single-word team name longer than max_len gets `full_name[:max_len]` which can produce nonsense like "Northweste".
**Fix:** Add an ellipsis or handle the single-word overflow case more gracefully.

---

### OBSERVATION #9: `_payout_calc` import is mid-file (line 790)

**Location:** `flow_sportsbook.py:790`
**Confidence:** 0.85
**Risk:** Mid-file imports break IDE navigation and can cause subtle reload issues. PEP 8 violation.
**Fix:** Move to top imports block.

---

### OBSERVATION #10: `import wager_registry` repeated 6+ times throughout the file

**Location:** lines 1153, 1357, 1503, 3614, 3838, 3871, 3930, 4050, 4062, 4073
**Confidence:** 0.95
**Risk:** Repeated local imports inside functions. Not a bug since Python caches imports, but verbose and easy to miss when refactoring.
**Fix:** Move to top imports block.

---

### OBSERVATION #11: `parlays_table.legs` column comment says "DEPRECATED" but schema and backfill still reference it

**Location:** `flow_sportsbook.py:191`, `3056`
**Confidence:** 0.85
**Risk:** Schema documentation drift. The column is still queryable and write-able in the schema, the backfill path still reads it. The deprecation notice has no enforcement.
**Fix:** Drop the column with a migration once all parlays have entries in `parlay_legs`. Until then, document the migration path explicitly.

---

### OBSERVATION #12: `MAX_PAYOUT = 10_000_000` defined but never enforced

**Location:** `flow_sportsbook.py:111`
**Confidence:** 0.95
**Risk:** Constant declared with comment "no single payout should exceed 10M" but `grep MAX_PAYOUT flow_sportsbook.py` finds only the declaration. There is no enforcement anywhere in this file. A bug elsewhere creating a degenerate parlay with 12 legs at +5000 each could trigger an absurd payout.
**Fix:** Apply the cap inside `_payout_calc` or at write_bet boundaries. Or delete the constant.

---

### OBSERVATION #13: `__init__` calls `setup_db()` which does ALTER TABLE migrations on every cog load

**Location:** `flow_sportsbook.py:3097`, `291-300`
**Confidence:** 0.85
**Risk:** Each cog load runs the migration block; the silent except is technically appropriate for "column already exists" but masks any other ALTER failures (locked DB, corrupt schema). Should be a one-shot migration helper.
**Fix:** Run migrations once at startup, not on every cog reload.

---

### OBSERVATION #14: `_expire_stale_cart_legs` is called from `__init__` synchronously

**Location:** `flow_sportsbook.py:3101`
**Confidence:** 0.8
**Risk:** Sync DB call in cog `__init__`. At cog load time the bot is single-threaded, so probably fine, but it's a pattern that doesn't scale. The same call is also wired into the daily snapshot loop (line 3112) using `asyncio.to_thread` — inconsistent.
**Fix:** Defer the startup expiration to the `before_loop` of `daily_snapshot` so it's consistently async.

## Cross-cutting Notes

- The reference_key idempotency contract is violated in MULTIPLE writers in this file (#1, #2, #3, #4). The pattern is consistent: settlement-side and cancel-side calls have it; placement-side and admin calls do not. This suggests a partial migration that left the placement and admin paths unfinished. A grep across the rest of the Flow ring will likely find similar gaps in `casino`, `economy_cog`, `flow_store`, and `real_sportsbook_cog`.
- The matchup-string vs gameId key mismatch (#5, #10) is a system-level architecture problem. The unified hub flow normalized to API gameIds but the legacy admin commands were never updated. ANY admin command that accepts a free-text matchup is suspect across the codebase. A second-pass refactor should introduce a typed `GameKey` resolver and ban string matchup parameters at the boundary.
- Silent except in admin views (#7, #8) is explicitly prohibited by CLAUDE.md, yet appears in two places in this file alone. A repository-wide grep for `except Exception:\s*\n\s*pass` in admin-flagged files would surface more.
- The two-DB mirror pattern (flow_economy.db ↔ flow.db, #6) is non-transactional by design and the cleanup paths are best-effort. This is not a one-file fix — the entire wager mirror layer needs a transactional outbox or a saga pattern to recover from partial failures. Until then, every place this pattern appears is a financial risk.
- `_compute_elo_ratings` and the derived caches (#4, #5, OBS#4) demonstrate that this file mixes thread-pool-executor work with asyncio callback work without thread safety. The pattern of "module-global cache + executor rebuild + async invalidation" is inherently unsafe; the codebase needs a single ownership model for shared compute caches.

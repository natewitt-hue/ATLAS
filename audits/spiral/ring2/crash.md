# Adversarial Review: casino/games/crash.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 541
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (3 critical, 7 warnings, 7 observations)

## Summary

The shared-round orchestration is reasonable and the TOCTOU guard on cashout (line 367) is correct in spirit, but the file leaks three real incident classes: (1) crash-on-tie the moment `current_mult >= crash_point` means the LAST player who races `_run_round` can get a fully-deducted wager yet be logged as a winner at the crash multiplier due to an ordering gap, (2) the error path in `_lobby_then_run` can double-refund a player who cashed out between detection and refund because the early abort path skips `resolve_crash_round`, and (3) `process_wager` for cashouts posts to the ledger only after the in-memory `cashed_out` flag is set, which means a crash that races the awaits mid-cashout will NOT cancel the payout but WILL mark the player as still active in the next render — fragile, but the biggest hole is that `deduct_wager` and `refund_wager` in the downstream `casino_db.py` call `flow_wallet.debit/credit` without `reference_key` at all, so every crash bet and every crash refund is vulnerable to Discord retry double-debits (CLAUDE.md hard rule violation, confirmed at `casino_db.py:1069` and `casino_db.py:1092`).

Additionally, the house-edge formula and its docstring disagree by ~2.5x in the "15x+" bucket, the view `timeout=None` plus `clear_items()` pattern leaks references on error, and `render_crash_card` is called every 2s which WILL trip Discord's 5-edit-per-5-seconds rate limit under sustained load (a known flow_live problem). Ship-blocking issues are the reference_key gap, the lobby-exit refund race, and the cashout ledger ordering.

## Findings

### CRITICAL #1: Double-refund race in `_lobby_then_run` exception handler

**Location:** `casino/games/crash.py:530-541`
**Confidence:** 0.85
**Risk:** When `_run_round` raises mid-round (Discord outage, render failure propagating, sqlite lock), the `except` clause iterates `round_obj.players` and refunds every bet that hasn't set `cashed_out = True`. However, the cashout handler at lines 367-370 sets `cashed_out = True` BEFORE awaiting `cashout_crash_bet` — so a player whose DB update finished successfully but whose `process_wager` credit then errored could be flagged `cashed_out=True` in memory AND have the wager credited twice: once via `cashout_crash_bet → process_wager → flow_wallet.credit` (payout + wager), and once more by a manual payout path if anyone retries. Worse, the opposite case: a player who was cashed out and whose `process_wager` DID credit them will be correctly skipped by the refund loop — BUT players who had NOT cashed out and whose `resolve_crash_round` had NOT been called yet (because the crash-loop crashed before line 228) will be refunded their wager while `crash_bets.status` remains `'active'`. The next run of `void_stale_crash_bets` will then refund them a SECOND time.
**Vulnerability:** Refund path does not (a) call `resolve_crash_round` to mark bets as `lost`/`voided`, (b) use a `reference_key` to make the credit idempotent (refund_wager itself omits it — see CRITICAL #3), or (c) track a "refunded" flag on the PlayerBet dataclass. A subsequent `void_stale_crash_bets` scan will see the still-`active` rows and issue a second refund.
**Impact:** Silent double-credit on any crash round that errors after lobby. House bank drift, unbounded economic exploit if an attacker can deliberately crash the loop (e.g. by spamming joins during the render window).
**Fix:** Before refunding, call `resolve_crash_round(round_obj.round_id)` with a mode that marks uncashed bets as `'voided'` (not `'lost'`). Pass a deterministic `reference_key` to `refund_wager`: `f"CRASH_REFUND_{round_obj.round_id}_{pbet.bet_id}"`. Track a `refunded: bool = False` flag on `PlayerBet` and skip already-refunded bets. And audit `void_stale_crash_bets` to ensure it too is reference-keyed.

### CRITICAL #2: `deduct_wager` and `refund_wager` omit `reference_key` (upstream contract violation)

**Location:** `casino/games/crash.py:466` (caller) → `casino/casino_db.py:1069, 1092` (sinks)
**Confidence:** 0.97
**Risk:** `join_crash` calls `deduct_wager(uid, wager, correlation_id=correlation_id)` at line 466. The downstream implementation at `casino_db.py:1069-1075` calls `flow_wallet.debit(..., subsystem_id=correlation_id, ...)` with NO `reference_key` argument. Per CLAUDE.md: "ALL debit calls MUST pass `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits." The `correlation_id` is only 8 hex chars (`uuid.uuid4().hex[:8]`, line 464) and is passed to `subsystem_id`, NOT `reference_key`. The `refund_wager` path at line 536 is worse — its backing function at `casino_db.py:1092-1096` doesn't even take a reference_key. Every refund is vulnerable to retry double-credit.
**Vulnerability:** Discord's `interaction.response.defer` + `followup.send` sequence can be retried by Discord's gateway on flaky connections, which will replay the entire `join_crash` coroutine. Without a `reference_key`, `flow_wallet.debit` has no deduplication key and will debit twice. Similarly the refund path in `_lobby_then_run` (line 536) has no key, so any transient failure-and-retry double-credits.
**Impact:** Financial ledger corruption on every Discord retry. Per CLAUDE.md this is a classified "hard-won lesson" — the rule exists because it happened before. House bank will drift silently.
**Fix:** In `casino_db.py:deduct_wager`, pass `reference_key=f"CASINO_DEBIT_{discord_id}_{correlation_id}"` to `flow_wallet.debit`. In `refund_wager`, add a required `reference_key` parameter and pass it to `flow_wallet.credit`. Update all callers of `refund_wager` in `crash.py` (line 536) and elsewhere to supply a deterministic key, e.g. `f"CRASH_REFUND_{round_id}_{bet_id}"`. This is the #1 casino rule in CLAUDE.md — file is non-compliant.

### CRITICAL #3: Distribution docstring is wrong — ~15% bucket is actually ~6.5%

**Location:** `casino/casino_db.py:1301-1333` (referenced from `crash.py:480, 486`)
**Confidence:** 0.92
**Risk:** The docstring claims `~15% beyond 15x`, but with `house_edge = 0.97` and `crash_point = 0.97/(1-p)`, crashing at ≥15x requires `p ≥ 1 - 0.97/15 = 0.9353`, which is ~6.5% of the uniform [0,1) draw — less than half the claimed rate. Similarly the "~35% crash before 2x" bucket is actually ~53.5% (p < 0.515). Every posted bucket is wrong. More dangerously, the `p < (1 - house_edge) = 0.03` instant-crash branch is claimed to be "very rare" but is 3% — 1 round in 33. With a 60-second lobby and tight player retention, that's a visible pattern and will erode trust.
**Vulnerability:** The formula was likely tuned against a mental model of the docstring, not the actual integral. House P&L accounting (`get_house_report` path) will diverge from what the ops team expects to see. Players studying the history (`recent_crashes` stores last 10) will notice the bias.
**Impact:** Trust loss (worst-case) if players compute the bias; P&L variance larger than expected; "it feels off" bug reports with no visible cause.
**Fix:** Either recalibrate the formula to match the docstring buckets, or update the docstring to the real distribution. At minimum, add a unit test that simulates 100k rounds and asserts the bucket rates match the documented buckets within tolerance. Also sanity-check: `MAX_CRASH_MULTIPLIER = 1000.0` (crash.py:50) disagrees with the hard cap `100.0` in `_generate_crash_point` (casino_db.py:1332). Pick one.

### WARNING #1: `_run_round` crash detection ignores already-cashed-out players in resolve ordering

**Location:** `casino/games/crash.py:179-228`
**Confidence:** 0.75
**Risk:** The while-loop exits as soon as `current_mult >= crash_point`, immediately setting `current_mult = crash_point` and breaking. However, a cashout button press that was fired just before the tick can be mid-`await cashout_crash_bet` at the moment the loop detects crash. Because the loop runs in a separate task from the button interaction, there is a window where: (a) player clicks cashout at 4.99x, (b) loop tick fires at 5.00x and crash_point is 5.00, (c) loop breaks, calls `resolve_crash_round`, which marks all `status='active'` rows as `'lost'`, (d) meanwhile the interaction's `cashout_crash_bet` runs its UPDATE — a classic last-writer-wins race. If (d) runs first, the row is `'cashed'` and resolve skips it (correct). If (c) runs first, the UPDATE in (d) still runs but now sets `cashout_mult` on a row already flagged `'lost'` — AND `process_wager` then credits the player, so the player gets paid AND the round shows them as lost. Double-state corruption.
**Vulnerability:** No coordination between the running-status check (line 359) and the DB update. The `player.cashed_out = True` pre-await guard (line 367) prevents in-process double-cashout, but it does not prevent the loop from concurrently resolving the same bet row.
**Impact:** Occasional double-pay or under-pay on cashouts that happen within one TICK_SECS of the crash. Player-visible "I cashed out but got nothing" or "I lost but got paid" bug reports.
**Fix:** In `cashout_crash_bet`, use a conditional UPDATE: `WHERE id=? AND status='active'` and check `cur.rowcount == 1`. If the UPDATE affected zero rows, the row was already resolved; return 0 and do not post a win. Also re-check `self.round_obj.status == "running"` AFTER the DB update and before calling `process_wager`.

### WARNING #2: `CrashView` is created per-round but `view=None` pattern never drops it cleanly

**Location:** `casino/games/crash.py:177, 205, 219-222, 316`
**Confidence:** 0.70
**Risk:** A single `CrashView` instance is bound to the running round and attached to the message on every edit at line 205. On crash, line 316 edits the message with the same (now-stopped) view, which leaves the dead buttons attached to the final render. Clicking the cashout button after crash triggers line 359's `status != "running"` check and returns a helpful message — good — but because `timeout=None` (line 343), the view instance is never garbage-collected until Discord's ~15 minute component timeout. Over a long session this leaks one CrashView per round.
**Vulnerability:** `view.clear_items()` is called in `finally` (line 222) but the view object itself is still referenced by the `discord.Message` until Discord's own timeout. `view=None` cannot be passed to `message.edit` per CLAUDE.md Discord constraint — must be omitted. Line 316 passes `view=view` on the crashed render; it should instead create a NEW message.edit call that omits `view` entirely to detach the components.
**Impact:** Memory pressure across a long uptime (thousands of crash rounds). Also, the stopped view's buttons still render as clickable UI chrome on the final post-crash image.
**Fix:** On the crashed render at line 316, call `round_obj.message.edit(attachments=[file])` with NO `view` argument at all — this detaches the view. Or, since clearing items leaves the view present, call `round_obj.message.edit(attachments=[file], view=discord.ui.View())` with an empty view. Either way, the dead CrashView should be dereferenced.

### WARNING #3: `_run_lobby` and `_run_round` edit the message with `attachments=[file]` every 2-5 seconds → Discord edit rate limit

**Location:** `casino/games/crash.py:155, 164, 205, 218`
**Confidence:** 0.90
**Risk:** Discord's message edit rate limit is 5 edits per 5 seconds per channel. `_run_lobby` edits every 5 seconds (safe), but `_run_round` edits every `TICK_SECS = 2.0` (line 48) seconds — that's 2.5 edits per 5 seconds, which is within limit for one round. HOWEVER: if multiple channels have crashes running in parallel AND the same channel has concurrent casino spam (blackjack, slots, flow_live highlights), the bucket is shared at the channel level for some rate types. Worse, `attachments=[file]` on EVERY edit sends a full PNG upload — no payload reuse, so each edit also hits the upload endpoint. Under load this manifests as intermittent `HTTPException: 429` which the code catches as generic `discord.HTTPException` and degrades silently to a text-only fallback (lines 156-163, 208-216) — the fallback is ANOTHER `channel.send` which also consumes rate-limit budget.
**Vulnerability:** The fallback-in-except-that-also-does-IO pattern can cascade: render fails → log.warning → `channel.send` fails → swallowed (`except discord.HTTPException: pass`, lines 162-163, 215-216). No circuit breaker, no backoff, no counter to stop the loop after N consecutive failures.
**Impact:** Degraded user experience during load spikes. In the worst case, the loop continues ticking and crashing on every single render until the round naturally ends, flooding logs with warnings.
**Fix:** Increase `TICK_SECS` to 3.0 or 4.0 (still feels live). On render failure, increment a counter on `round_obj`; if the counter exceeds 3 consecutive failures, break the loop and fail the round cleanly (refund everyone via the same path as CRITICAL #1). Track last successful edit time and back off exponentially on 429s.

### WARNING #4: Silent `except discord.HTTPException: pass` on fallback text sends

**Location:** `casino/games/crash.py:162-163, 215-216, 328-329`
**Confidence:** 0.95
**Risk:** Three locations silently swallow HTTPException on the text-fallback `channel.send`. Per CLAUDE.md, silent `except: pass` in admin-facing views is PROHIBITED. Casino views qualify — the ledger, payouts, and affinity are all operator-visible. A swallow here hides bugs like: channel deleted mid-round, bot kicked from guild, permission revoked. The round continues, the DB state accumulates, and operators see no notification.
**Vulnerability:** No `log.exception(...)`, no metric, no admin-channel notification. The round will complete its DB lifecycle (resolve, log losses) but the channel users will see nothing after the initial render failure.
**Impact:** Silent failures during crashes at the Discord edge. Operators cannot triage from logs alone.
**Fix:** Replace each `except discord.HTTPException: pass` with `log.exception("Crash fallback send failed in channel %s", round_obj.channel_id)`. Consider posting to the admin channel via `setup_cog.get_channel_id("admin")` if multiple failures accumulate.

### WARNING #5: `_lobby_then_run` early-returns on `round_obj.players` empty without cleanup

**Location:** `casino/games/crash.py:525-527`
**Confidence:** 0.80
**Risk:** If after the lobby all players have somehow been removed (no current path does this, but `void_stale_crash_bets` could theoretically), the function returns at line 527 without calling `resolve_crash_round` to close the `crash_rounds` row. The `finally` at line 540 pops the channel from `active_rounds`, but the `crash_rounds.status='open'` row persists in SQLite forever. `void_stale_crash_bets` (referenced from line 1400 in casino_db.py) cleans up `crash_bets` after 30 minutes but does not touch `crash_rounds`.
**Vulnerability:** Orphan rows in `crash_rounds` table. Accumulates indefinitely.
**Impact:** Slow table growth, weird admin queries like "how many rounds did we run this season" return inflated numbers.
**Fix:** Before the `return` at line 527, call `await resolve_crash_round(round_obj.round_id)` (or a dedicated `void_crash_round`) to mark status as `'voided'`.

### WARNING #6: `cashout_crash_bet` returns 0 on error but crash.py still logs a "win" session

**Location:** `casino/games/crash.py:370-398`
**Confidence:** 0.85
**Risk:** `cashout_crash_bet` (casino_db.py:1372) returns 0 if the row doesn't exist or status isn't 'active'. In crash.py at line 370, `payout = await cashout_crash_bet(...)` silently accepts `payout=0` and then calls `process_wager` with `outcome="win", payout=0, multiplier=mult`. This logs a "win" with zero payout — the player has already been charged the wager, the credit never happens because `payout=0`, so the player just lost the round and was told "✅ cashed out at X.XXx, +$-wager!" (profit calculation at line 400 is `payout - player.wager = 0 - wager = -wager`).
**Vulnerability:** No check for `payout == 0` after `cashout_crash_bet`. The in-memory `player.cashed_out = True` guard fired pre-await (line 367), so on retry the code says "you already cashed out" — the error is irrecoverable and silent.
**Impact:** Player sees a confusing "cashed out at 5x, +$-1000" message and their balance doesn't change. A rare bug class, but bad UX when it triggers.
**Fix:** After the `cashout_crash_bet` call, check `if payout <= 0: revert player.cashed_out and player.cashout_mult; send ephemeral error; return`. Also log the condition with `log.error` so operators can investigate DB drift.

### WARNING #7: `post_to_ledger` imported inside functions (3x) — fragile and slow

**Location:** `casino/games/crash.py:250-251, 261, 391`
**Confidence:** 0.60
**Risk:** `from casino.casino import post_to_ledger` is imported inside function bodies three times: in LMS bonus path (250-251), in loss-logging loop (261), and in cashout handler (391). Each import goes through `sys.modules` cache so it's not catastrophic, but it indicates circular-import workarounds. If `casino.casino` import fails (e.g. partial load during reconnect), the crash round will raise ImportError mid-execution at a random point, leaving state partially committed. This is deferred failure — it won't show up during startup testing.
**Vulnerability:** Hides circular dependency. The reason for in-function imports is usually "casino.casino imports crash.py at top level", which can cause deadlocks during module load.
**Impact:** Fragile contract. Refactoring `casino.casino` can break crash rounds in non-obvious ways.
**Fix:** Hoist the import to module top-level behind a `TYPE_CHECKING` or late-binding helper. Or use `importlib.import_module("casino.casino").post_to_ledger` once at file load with a fallback stub.

### OBSERVATION #1: `MAX_CRASH_MULTIPLIER = 1000.0` disagrees with casino_db.py hard cap `100.0`

**Location:** `casino/games/crash.py:50` vs `casino/casino_db.py:1332`
**Confidence:** 0.95
**Risk:** `_current_multiplier` caps at 1000.0, but `_generate_crash_point` caps crash_point at 100.0. So the live multiplier climb (`1.06 ** elapsed`) can theoretically reach 1000.0 visually, but the round WILL always crash at or before 100.0 due to the crash_point cap at line 184. The MAX_CRASH_MULTIPLIER constant is dead.
**Vulnerability:** Dead constant suggests the curve was refactored but the cap wasn't cleaned up.
**Impact:** Cosmetic / code hygiene.
**Fix:** Delete `MAX_CRASH_MULTIPLIER = 1000.0` or set it to 100.0 to match the source of truth. Also remove the `min(round(1.0 * (1.06 ** elapsed), 2), MAX_CRASH_MULTIPLIER)` cap in `_current_multiplier` since crash_point will always trigger first.

### OBSERVATION #2: Near-miss detection uses hardcoded magic numbers

**Location:** `casino/games/crash.py:61-75`
**Confidence:** 0.85
**Risk:** Thresholds `0.5`, `1.5`, `2.0` are hardcoded. The margin 0.5x for "barely escaped" is reasonable but not tuned against actual round data. More importantly: the function returns a message string but the return value is never used anywhere in this file — `_detect_crash_near_miss` is called at line 266 and stored in `near_miss_msg`, then never referenced. Dead assignment.
**Vulnerability:** Dead code. Near-miss detection was wired but the message path was never completed.
**Impact:** Feature incompleteness. Players who barely escaped see no indication; the detection function runs and its output is discarded.
**Fix:** Either plumb `near_miss_msg` into the render card (extra caption field) and/or `post_to_ledger` extra dict, or delete `_detect_crash_near_miss` entirely.

### OBSERVATION #3: `crash_point` tie breaks at exact `current_mult == crash_point`

**Location:** `casino/games/crash.py:184`
**Confidence:** 0.70
**Risk:** The condition `current_mult >= crash_point` means on exact equality (e.g. tick lands on 5.00 and crash_point is 5.00), the round crashes and no player can cash out at exactly 5.00x. Given that `current_mult` is `round(..., 2)` and `crash_point` is also `round(..., 2)`, the exact-hit probability is low but non-zero. Players who pre-registered a cashout at 5.00x (no such feature exists, but UI polish could add one) would always lose ties.
**Vulnerability:** Ordering assumption — ties break against the player, which is correct for casino house-edge but undocumented.
**Impact:** Undocumented fairness convention. Not a bug, but a player complaint vector.
**Fix:** Add a docstring note: "Ties at exact crash_point resolve as a crash (house-favored)."

### OBSERVATION #4: `recent_crashes` has no per-channel lock — dict mutation race

**Location:** `casino/games/crash.py:54, 295-298`
**Confidence:** 0.55
**Risk:** `recent_crashes` is a module-level dict of `channel_id → list[float]`. Two concurrent rounds (if the guard at line 445 ever allows it) or a stale render-loop appending while the crashed-loop's final store runs (lines 295-298) could interleave list mutations. Python's GIL makes `list.append` and `list.pop(0)` atomic individually, so it's not a crash bug, but the ordering could produce a torn history.
**Vulnerability:** Shared mutable state without a lock. Current code paths don't actually race (single round per channel guard), but if the concurrency model changes this becomes a data-integrity bug.
**Impact:** Low — current paths safe. Risk is in future refactors.
**Fix:** Either add a comment explaining the invariant ("only mutated by the running round's final block"), or use `collections.deque(maxlen=10)` which has proper bounded semantics without pop-in-place.

### OBSERVATION #5: `active_rounds` uses string sentinel `"PENDING"` mixed with CrashRound objects

**Location:** `casino/games/crash.py:53, 440, 478, 489`
**Confidence:** 0.80
**Risk:** `active_rounds: dict[int, CrashRound]` is annotated as `dict[int, CrashRound]` but line 478 stores the string `"PENDING"` as a sentinel. The `isinstance` check at line 440 is correct but the type annotation is a lie. Type checkers (mypy, pyright) will flag every access of `.status` as unsafe.
**Vulnerability:** Type confusion. Easy for a future refactor to call `existing.status` without first checking `isinstance(existing, CrashRound)`.
**Impact:** Static-type unsound; latent AttributeError.
**Fix:** Either introduce a sentinel enum value in CrashRound (e.g. `CrashRound.pending()` classmethod returning an object with `status='pending'`), or change the annotation to `dict[int, CrashRound | Literal["PENDING"]]` and add type guards.

### OBSERVATION #6: `asyncio.create_task(_lobby_then_run(...))` has no reference retention

**Location:** `casino/games/crash.py:502`
**Confidence:** 0.85
**Risk:** The created task has no strong reference held outside the function. Per Python docs, `asyncio.create_task` returns a task whose reference is held only by the event loop; if the loop garbage-collects task refs (does not happen currently but Python 3.11+ warns about this pattern), the task can be cancelled mid-round.
**Vulnerability:** Known Python asyncio footgun. `asyncio.create_task` return values should be stored in a module-level set to prevent premature GC.
**Impact:** Possible silent task cancellation. Has not yet happened, but is a known footgun for long-running asyncio applications.
**Fix:** Add module-level `_background_tasks: set[asyncio.Task] = set()`, store with `task = asyncio.create_task(...); _background_tasks.add(task); task.add_done_callback(_background_tasks.discard)`.

### OBSERVATION #7: LMS bonus sends `wager=0` through `process_wager` — distorts house-bank accounting

**Location:** `casino/games/crash.py:239-258`
**Confidence:** 0.70
**Risk:** The LMS bonus calls `process_wager(wager=0, outcome="win", payout=lms_bonus, multiplier=0.10)`. Zero wager with a payout is not a wager at all — it's a bonus credit. `process_wager`'s house-bank delta math (which typically computes `house_delta = wager - payout`) will treat this as `0 - lms_bonus = -lms_bonus`, correctly subtracting from the house bank. BUT the session log will record a "win" with wager=0, multiplier=0.10 (wrong — multiplier should be cashout_mult or 1.10 representing the 10% add), which distorts any "multiplier distribution" or "average wager" analytics.
**Vulnerability:** Wrong semantics for LMS bonus. Should be a direct credit with a dedicated game_type, not a fake wager.
**Impact:** Analytics and post-hoc reporting drift. Not a financial bug (house bank math is correct).
**Fix:** Add a dedicated `credit_bonus` function in `casino_db.py` that writes a direct ledger entry with a `game_type='crash_lms'`, a `reference_key=f"CRASH_LMS_{round_id}_{player_id}"`, and skips the session-log distortion. Also use the actual LMS multiplier (1.10) rather than `0.10` for display.

## Cross-cutting Notes

- The `deduct_wager` / `refund_wager` reference_key gap (CRITICAL #2) applies to ALL casino games that call these functions — blackjack, slots, coinflip, etc. The fix belongs in `casino_db.py`, not in `crash.py`. Flag this at Ring 2 level; audit every caller in the casino subsystem.
- The silent `except HTTPException: pass` pattern (WARNING #4) and the "edit every N seconds with full attachments" pattern (WARNING #3) almost certainly exist in `casino/renderer/pulse_renderer.py`, `session_recap_renderer.py`, and any other live-update renderer. A subsystem-wide fix is warranted: implement a `RateLimitedEdit` helper that batches/throttles edits and raises meaningful exceptions.
- The `post_to_ledger` late-import pattern (WARNING #7) indicates a circular dependency between `casino.casino` and `casino.games.*`. Fix structurally: create `casino/ledger.py` with `post_to_ledger` and import from both sides.
- The "dead constant" issue (OBSERVATION #1) and the "dead function return" issue (OBSERVATION #2) together indicate an unfinished feature sweep — someone refactored near-miss detection and crash caps but didn't clean up callers. Worth a full-file dead-code pass.

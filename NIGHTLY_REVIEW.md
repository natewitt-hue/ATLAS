# ATLAS Nightly Audit — 2026-04-07

**Focus:** Casino & Rendering | **Recent commits:** 2 | **Files deeply read:** 17 | **Total lines analyzed:** ~10,215

---

## CRITICAL

### C-01 — Deadlock in `atlas_html_engine._get_browser()` after browser reconnect
**File:** `atlas_html_engine.py` · `_get_browser()` / `PagePool.warm()`
**Severity:** CRITICAL — all renders hang permanently after any browser crash/reconnect

`_get_browser()` acquires `_browser_lock` (a non-reentrant `asyncio.Lock`), launches the browser, then calls `await _pool.warm()` **while still holding the lock**. `warm()` calls `_new_page()` → `_get_browser()` → `async with _browser_lock` → deadlock. The event loop is blocked forever; no casino render can complete.

```python
# CURRENT (broken):
async def _get_browser():
    async with _browser_lock:          # lock acquired
        ...
        _browser = await _pw_instance.chromium.launch(headless=True)
        if _pool is not None:
            await _pool.warm()         # re-enters _get_browser() → tries _browser_lock → DEADLOCK
```

**Fix — move `_pool.warm()` outside the lock:**
```python
async def _get_browser():
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            if _pool is not None:
                try:
                    await _pool.drain()
                except Exception:
                    pass
            _browser = await _pw_instance.chromium.launch(headless=True)
    # warm AFTER releasing lock — _new_page() can now call _get_browser() safely
    if _pool is not None:
        await _pool.warm()
```

This bug is latent in normal operation (browser stays alive) but is guaranteed to trigger on any Playwright crash, OOM kill, or bot restart that reconnects to an existing browser session. The orphan reconciliation loop and any render that fires in the reconnect window will deadlock all four pool pages simultaneously.

---

## WARNINGS

### W-01 — Streak bonus idempotency key collision in `casino_db.process_wager()`
**File:** `casino/casino_db.py` · L949
**Severity:** WARNING — legitimate streak bonuses silently blocked on re-achievement

Reference key: `f"streak_bonus_{discord_id}_{game_type}_{streak_info['len']}"` is constructed without a timestamp or session ID. If a player loses their W3 streak and rebuilds it to W3 again, `flow_wallet.credit()` sees the same key and skips the bonus as an idempotency duplicate. The player earned the bonus legitimately but never receives it.

**Fix:** Append epoch seconds or session ID:
```python
ref_key = f"streak_bonus_{discord_id}_{game_type}_{streak_info['len']}_{int(time.time())}"
```
The idempotency window for streak bonuses is one credit event, not one streak length. A unique timestamp suffix preserves protection against double-click races while allowing re-achievement payouts.

---

### W-02 — UTC vs local time split in streak tracking
**File:** `casino/casino_db.py` · `get_streak()` L612, `_update_streak()` L629
**Severity:** WARNING — streak resets behave differently depending on server timezone; off by one day at midnight UTC

`get_streak()` and `_update_streak()` use `date.today().isoformat()` (local server time). The scratch functions (`can_claim_scratch`, `get_scratch_streak`, `claim_scratch`) were correctly fixed to `datetime.now(timezone.utc).date()` in the recent sicko-mode commit, but the base streak functions were not updated to match.

On a UTC+X server, streaks reset at a different wall-clock time than scratch claims. A player who wins at 11:45 PM local / 00:15 AM UTC will see their streak treated as day N while scratch treats it as day N+1.

**Fix:** Replace `date.today()` with `datetime.now(timezone.utc).date()` in both `get_streak()` and `_update_streak()`. Requires `from datetime import datetime, timezone` already present in the module.

---

### W-03 — `void_stale_crash_bets()` defined but never called (dead code)
**File:** `casino/casino_db.py` · L1400 (definition); `casino/casino.py` (no caller found)
**Severity:** WARNING — stale crash bets from aborted rounds accumulate indefinitely

The function was added in the recent commit but has zero callers. Confirmed via codebase-wide grep: only the definition exists. Crash bets that were deducted but never settled (e.g., bot restart mid-round) will sit as open debits in the wager registry with no automatic resolution, which confuses the orphan reconciliation loop and skews ledger reports.

**Fix:** Wire into `CasinoCog._orphan_reconciliation_loop()` alongside `reconcile_orphaned_wagers()`:
```python
async def _orphan_reconciliation_loop(self):
    while True:
        await asyncio.sleep(600)
        await reconcile_orphaned_wagers()
        await void_stale_crash_bets()   # ADD THIS
```

---

## OBSERVATIONS

### O-01 — `db_migration_snapshots.py` blocking task template would stall event loop
**File:** `db_migration_snapshots.py` · task template comment block
**Severity:** LOW — not active in production; latent if wired up

The commented-out task template calls `take_daily_snapshot()` directly from async context. `take_daily_snapshot()` uses synchronous `sqlite3.connect()` with no `asyncio.to_thread()` wrapper. If the template is activated as a scheduled cog task, it will block the event loop for the full duration of a DB snapshot (~100ms–1s depending on size). Low priority but worth noting before FROGLAUNCH where scheduling will be revisited.

---

### O-02 — `play_again.py` silently swallows wager clamping path error
**File:** `casino/play_again.py` · `_on_play()` L112–116
**Severity:** LOW — functional but fragile

When `actual_wager < self.wager`, the clamped replay path uses `functools.partial` on `self.replay_callback.func` — but `replay_callback` may not be a `functools.partial` object (it could be a plain coroutine function). Accessing `.func` on a plain callable raises `AttributeError`, which is not caught. The interaction is already partially consumed (buttons disabled via `_disable_all()`), leaving the player in a dead state with no game started and no error shown.

**Fix:** Check type before accessing `.func`, or pass wager as a keyword argument through the callback signature consistently.

---

### O-03 — `PagePool` silently shrinks on `_new_page()` failure
**File:** `atlas_html_engine.py` · `PagePool.release()`
**Severity:** LOW — no data loss; degrades render throughput over time

When `_new_page()` raises (e.g., browser context timeout), `release()` logs the error and returns without re-adding a replacement page. The pool permanently loses that slot. Under sustained load, pool size drifts from 4 → 3 → 2 → 1 with no alerting, causing render queue backups. The browser-reconnect deadlock (C-01) makes this worse: if the fix for C-01 is applied, `warm()` after reconnect will replenish, but any pre-reconnect failures still shrink the pool.

---

## CROSS-MODULE RISKS

| Risk | Files | Details |
|------|-------|---------|
| Rendering hard-blocked by C-01 | `atlas_html_engine.py` ↔ all renderers | All 6 renderer modules call `render_card()` which depends on the pool. C-01 means a single browser reconnect takes down casino, predictions, ledger, and pulse simultaneously. |
| Streak bonus silently blocked (W-01) | `casino_db.py` ↔ `flow_wallet.py` | Idempotency key collision means `flow_wallet.credit()` rejects the bonus with no error surface to the player. The wager resolves successfully, so the bug is invisible in logs unless flow audit is queried directly. |
| Crash orphan accumulation (W-03) | `casino_db.py` ↔ `wager_registry` ↔ `reconcile_orphaned_wagers()` | `void_stale_crash_bets()` targets crash-specific open bets. Without it, `reconcile_orphaned_wagers()` may or may not catch these depending on how crash bets are tagged — there is a risk of double-refund if reconciliation runs on crash bets that `void_stale_crash_bets()` would also close. Caller order in the maintenance loop matters. |
| UTC split creates cross-subsystem date boundary inconsistency | `casino_db.py` streak vs scratch | Two subsystems in the same module disagree on when "today" ends. Any feature that reads both streak and scratch state for the same player on a date boundary can produce contradictory data. |

---

## POSITIVE PATTERNS

These patterns are well-implemented and worth preserving explicitly.

| Pattern | Location | Why It's Good |
|---------|----------|---------------|
| TOCTOU sentinel before first `await` | `blackjack.py`, `crash.py`, `coinflip.py` | Sets in-memory state synchronously before any DB/Discord call, preventing double-execution across concurrent Discord interactions. The standard pattern for Discord UI race conditions. |
| `BEGIN IMMEDIATE` in `process_wager()` | `casino_db.py` | Prevents TOCTOU between balance check and debit in SQLite WAL mode. Correct choice — `BEGIN DEFERRED` would allow a race window. |
| Outcome-first RTP in slots | `casino/games/slots.py` | `_roll_outcome()` → `_generate_reels_for_outcome()` decouples payout math from visual generation. RTP is deterministic regardless of visual; visual can be changed without touching payout logic. |
| Weight total assertion at module load | `casino/games/slots.py` | `assert _actual_weight_total == 80` fails fast at import time if `SLOT_ICON_CONFIG` drifts. No silent RTP miscalibration ever ships. |
| `finally` page return in `render_card()` | `atlas_html_engine.py` | Page is always returned to pool even on exception, preventing permanent pool exhaustion from render errors. |
| `OrderedDict` LRU font cache | `atlas_html_engine.py` | Bounded at 50 entries with O(1) eviction. No unbounded memory growth in long-running sessions. |
| Streak bonus and cold refund as separate idempotent credits | `casino_db.py` | Separating these from the main wager credit means each can be independently retried or skipped without re-running the full wager resolution. Correct decomposition of the settlement path. |

---

## TEST GAPS

| Gap | Risk |
|-----|------|
| No test for `_get_browser()` re-entry path (C-01) | The deadlock only manifests on browser reconnect — hard to hit manually, easy to miss in review. Needs a mock that forces `_browser.is_connected()` to return False. |
| No test for streak re-achievement bonus (W-01) | Would require a player to build, break, and rebuild a streak to the same length. Current tests likely only test first-time achievement. |
| No test that `void_stale_crash_bets()` is invoked by the maintenance loop (W-03) | Function existence test alone isn't sufficient — need integration test confirming it's called. |
| No UTC boundary test for streak functions | Need to assert `get_streak()` uses UTC date, not local. |
| `play_again.py` wager clamping path (O-02) | The `functools.partial` `.func` access is only triggered when wager is clamped — requires a test that changes `max_bet` between game creation and replay. |

---

## METRICS

| Metric | Value |
|--------|-------|
| Critical findings | 1 |
| Warnings | 3 |
| Observations | 3 |
| Cross-module risks | 4 |
| Positive patterns documented | 7 |
| Test gaps | 5 |
| Files read | 17 |
| Lines analyzed | ~10,215 |
| Commits in scope | 2 (`d9cf9e2`, `d00007f`) |
| New functions with no callers | 1 (`void_stale_crash_bets`) |
| Idempotency violations | 1 (streak bonus key collision) |
| UTC consistency violations | 1 (streak date functions) |

---

*Audit completed autonomously by scheduled task `audit-tuesday-casino` · 2026-04-07*

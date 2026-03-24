# ATLAS Nightly Code Review
**Date:** 2026-03-24
**Scope:** Casino & Rendering Subsystem
**Auditor:** Claude Code (automated overnight audit)
**Files audited:** 15 files · 11,705 lines

---

## CRITICAL

### C-1 · `_award_jackpot` return type mismatch — `casino/casino_db.py` L495–548

**Severity:** HIGH

`_award_jackpot` is annotated as returning `dict` but returns `None` at the early-exit path when `amount < 1`. The caller `process_wager` stores the result and may pass it downstream to streak/highlight logic that accesses dict keys without a None guard. This will surface as an unhandled `TypeError` mid-transaction, silently failing a payout with no user-visible error message.

**Fix:** Change the annotation to `dict | None` and add a None check at all call sites, or return an empty sentinel dict `{}` and guard on `jackpot_info.get("amount", 0)`.

---

### C-2 · `get_challenge` column key zip is fragile against schema drift — `casino/casino_db.py` L1419–1430

**Severity:** HIGH

`get_challenge` uses `SELECT *` followed by a hardcoded list of column names to zip into a dict. The DDL confirms `challenger_corr_id` and `opponent_corr_id` were added via `ALTER TABLE` after initial creation. On any production database where the ALTER was applied in a different order, SQLite column ordering may differ from what the zip expects. A misaligned zip produces silently wrong data: money may be credited to the wrong user or correlation IDs swapped, corrupting the ledger without raising an exception.

**Fix:** Replace with an explicit named-column `SELECT` (e.g. `SELECT id, challenger_id, opponent_id, ...`). Never rely on `SELECT *` positional ordering when the schema has been modified via `ALTER TABLE`.

---

## WARNINGS

### W-1 · `post_to_ledger` silently swallows all exceptions — `casino/casino.py` L49–82

`post_to_ledger` wraps both the ledger DB write and the FLOW event emission in a bare `except Exception: log.exception(...)`. Ledger failures are invisible to the player and to Discord. Wagers can be resolved but never recorded in the transaction ledger — a bookkeeping gap that cannot be detected without manually diffing wager outcomes against ledger entries.

**Recommendation:** Emit a warning to the configured admin channel on ledger write failure.

---

### W-2 · `_is_admin` bypasses shared permission helper — `casino/casino.py` L87–93

`CasinoCog._is_admin()` checks `ctx.author.guild_permissions.administrator` and a hardcoded `"Commissioner"` role name directly, bypassing `is_commissioner()` from `permissions.py`. The rest of the bot uses `permissions.py` which also checks `ADMIN_USER_IDS` from env. A commissioner configured only in `ADMIN_USER_IDS` will be denied access to casino commissioner commands.

**Fix:** Replace with `from permissions import is_commissioner; return await is_commissioner(ctx)`.

---

### W-3 · `my_stats` handler uses raw `aiosqlite.connect` instead of DB layer — `casino/casino.py` L275–330

The `my_stats` button handler opens `aiosqlite.connect(db.DB_PATH)` directly, bypassing `casino_db` entirely. This bypasses the locking layer (`flow_wallet.get_user_lock()`), runs outside any `BEGIN IMMEDIATE` context, and will not benefit from future schema migrations applied through the DB layer.

**Recommendation:** Extract the stats query into a `casino_db` function.

---

### W-4 · `assert` in production code path — `casino/renderer/ledger_renderer.py` L283

```python
assert info is not None
```

Python `assert` is a no-op when run with the `-O` (optimize) flag. Even when active, it raises `AssertionError` rather than a graceful error. The rendering caller catches `Exception` (which includes `AssertionError`), so the card render will fail silently.

**Fix:** Replace with `if info is None: raise ValueError(f"Unknown ledger source: {source_key}")`.

---

### W-5 · Playwright page pool zombie-check iterates stale `page` reference — `atlas_html_engine.py` L604–613

The `PagePool.acquire()` health-check loop retries up to `self._size` times but reuses the same `page` variable without fetching a fresh candidate from the queue at each iteration. In the failure scenario where one page in the pool is closed, this loop spins `_size` times on the same closed page before falling through to spawn a replacement — a latency spike under concurrent load with multiple closed pages.

**Recommendation:** Re-fetch from the queue at each iteration, or restructure to drain-and-replace individually.

---

### W-6 · `asyncio.sleep` calls extend the interaction response window — multiple game files

`blackjack.py:532` (1.5s), `slots.py` (3× 0.9s = 2.7s minimum per spin), `crash.py` (multiple). These yield control to the event loop and are not true blocking calls, but they extend the Discord interaction response window. In `slots.py`, the 2.7s animation sleep plus Playwright render time approaches the 3s Discord interaction timeout window on slow hardware.

---

### W-7 · `_get_browser()` singleton has no initialization lock — `atlas_html_engine.py`

The global `_browser` singleton uses a bare `if _browser is None: _browser = await ...` pattern with no `asyncio.Lock`. Under concurrent startup (two cogs triggering renders simultaneously before the pool is ready), two browser instances can be spawned and the second overwrites the first, leaving the first as a zombie process consuming system resources.

**Recommendation:** Guard initialization with an `asyncio.Lock` stored at module level.

---

## OBSERVATIONS

### O-1 · Dead code block in `check_achievements` — `casino/casino_db.py` L770–773

A cursor is opened with `async with conn.execute("SELECT ...")` and immediately discarded with `pass`. This is a no-op query that wastes a round-trip to SQLite on every wager resolution. The achievement system is a stub that was never implemented.

---

### O-2 · Redundant `get_max_bet()` call in coinflip — `casino/games/coinflip.py` L79 and L152

`play_coinflip` fetches `max_bet` at L79 for display purposes, then `ChallengeView.accept()` fetches it again at L152 before processing the wager. An unnecessary second DB round-trip; the two calls can theoretically return different values if bet limits change mid-session.

---

### O-3 · `coin_gradient`/`coin_rim` CSS values injected as raw f-string — `casino/renderer/casino_html_renderer.py` L1700

Values are constructed from internal logic (not user input), so no practical XSS vector exists today. Noted for future-proofing: if the source ever changes to include user-supplied data, the lack of escaping becomes a real risk.

---

### O-4 · Crash round processes O(n) individual DB writes for multi-player rounds — `casino/games/crash.py`

For each losing player in a crash round, `process_wager()` is called individually inside the `_run_round()` loop. Each call acquires `flow_wallet.get_user_lock()` and opens a `BEGIN IMMEDIATE` transaction separately. With 10+ concurrent crash participants, this serializes O(n) exclusive DB writes at end-of-round, creating a burst of lock contention.

---

### O-5 · `highlight_renderer.py` defines a local `_wrap_card` that shadows the engine's `wrap_card` — L82

Scoped correctly and does not cause a collision, but a future maintainer editing this file may be surprised that `_wrap_card` is not the same function as `wrap_card`. A comment or rename would improve clarity.

---

### O-6 · `biggest_loss` display relies on undocumented sign convention — `casino/renderer/session_recap_renderer.py` L241

Correctness depends on whether `PlayerSession.biggest_loss` stores losses as a negative integer or a positive loss magnitude. If the convention changes, `abs()` silently displays the wrong sign.

---

### O-7 · Scratch card uses `status_class = "win"` for all-revealed non-match outcomes — `casino/renderer/casino_html_renderer.py` L1941–1944

When all 3 tiles are revealed with no triple match, the card renders with a green `"win"` status bar, visually indistinguishable from a jackpot result. Minor UX distinction issue — no functional bug.

---

### O-8 · `pulse_renderer.py` footer "Updates every 60s" is hardcoded — L395

If the calling cog changes the refresh interval, the displayed value will be stale. Should be passed as a parameter.

---

## CROSS-MODULE RISKS

### XM-1 · `flow_wallet` shared DB — sportsbook settle cycle could block casino wagers

`sportsbook.db` is shared between `casino_db.py` and `flow_sportsbook.py`. If `flow_sportsbook.py` ever opens a `BEGIN EXCLUSIVE` transaction during a settle cycle, it will block all casino wager deductions for the duration. Currently using `BEGIN IMMEDIATE` (correct). Risk is latent as sportsbook settle complexity grows.

---

### XM-2 · `reconcile_orphaned_wagers` 10-minute loop may flag in-flight blackjack hands as orphans

If a player has a blackjack hand open for more than 10 minutes (Discord view timeout is 5 minutes but hands can be left open), the wager appears orphaned to the reconcile loop. The `on_timeout()` handler resolves the wager on timeout, but the race window exists between the timeout firing and the reconcile loop's `NOT EXISTS` query.

---

### XM-3 · Prediction renderer imports `CATEGORY_COLORS_HEX` from `polymarket_cog` with a try/except fallback

If `polymarket_cog` is not loaded (disabled in cog load order), the renderer silently uses a fallback color dict. Prediction cards rendered during polymarket cog absence will have different category colors than when it is loaded — visual inconsistency with no error raised.

---

### XM-4 · Ledger write failure (W-1) is invisible to `reconcile_orphaned_wagers`

If `post_to_ledger` fails silently, the wager is marked resolved in `wager_registry` but has no corresponding ledger entry. The reconcile pass will not catch this because the wager shows as resolved — the missing ledger post is permanently invisible to the reconcile system.

---

## POSITIVE PATTERNS

1. **SENTINEL pattern used correctly in all concurrent flows.** `active_sessions["PENDING"]`, `active_rounds[ch_id] = "PENDING"`, and `ChallengeView.resolved = True` are all set before the first `await` in their respective paths. This correctly prevents TOCTOU double-entry without requiring a separate lock.

2. **`BEGIN IMMEDIATE` used consistently for all balance-modifying operations.** `process_wager`, `deduct_wager`, and jackpot award all use `BEGIN IMMEDIATE` transactions with explicit rollback on exception. The locking hierarchy (user-lock then BEGIN IMMEDIATE) is consistent throughout `casino_db.py`.

3. **RNG is always server-side and outcome-first.** In slots, the outcome is computed by `_spin_controlled()` before any animation renders. In crash, the crash point is computed via SHA-256 hash of a server secret before players join. In blackjack, the shoe is shuffled server-side. No client-visible state can be used to predict outcomes.

4. **All user-facing text passes through `esc()` (html.escape) in every renderer.** Checked across all 7 renderer files. No unescaped f-string interpolation of user-supplied data was found.

5. **Playwright page pool uses `try/finally` to guarantee page return.** `render_card()` in `atlas_html_engine.py` wraps every render in `try/finally: pool.release(page)`, ensuring pages are never permanently removed from the pool even on render failure.

6. **`atlas_style_tokens.py` is a genuine single source of truth.** All 15 renderer files reference CSS variables generated from `Tokens._CSS_MAP`. No hardcoded color hex values found outside the tokens file and the `atlas_themes.py` override system.

7. **Blackjack `on_timeout()` correctly handles split hands** by iterating all pending hands and resolving each wager individually, preventing money loss on Discord interaction timeout.

8. **Parameterized queries used throughout `casino_db.py`.** Zero SQL string formatting with user data found. All variable data is passed as SQLite parameter tuples.

---

## TEST GAPS

- No unit tests for `_award_jackpot` return type contract — the `dict | None` mismatch (C-1) would be caught immediately by a type assertion test.
- No integration test for `get_challenge` column ordering after `ALTER TABLE` (C-2) — a test that creates a challenge DB with altered column order then calls `get_challenge` would expose the zip misalignment.
- No test for `reconcile_orphaned_wagers` with a concurrent in-flight wager (XM-2).
- No smoke tests confirming all 7 renderers produce non-empty PNG bytes — a CSS syntax error in any renderer would only surface when a user triggers that specific game.
- No test for `post_to_ledger` failure path (W-1) — the silent swallow produces no observable signal.
- No test for Playwright pool exhaustion (all 4 pages checked out simultaneously).

---

## METRICS

| Metric | Value |
|--------|-------|
| Files read | 15 |
| Total lines audited | 11,705 |
| CRITICAL findings | 2 |
| WARNING findings | 7 |
| OBSERVATIONS | 8 |
| CROSS-MODULE RISKS | 4 |
| SQL injection vectors found | 0 |
| Unescaped HTML injection vectors found | 0 (1 low-risk internal CSS injection noted in O-3) |
| `eval`/`exec` calls found | 0 |
| `SELECT *` with positional zip | 1 (C-2) |
| Bare `assert` in production paths | 1 (W-4) |
| `asyncio.sleep` calls in interaction handlers | 6 total (BJ: 1, Slots: 3, Crash: multiple) |
| `BEGIN IMMEDIATE` transaction sites | 7 |
| SENTINEL anti-TOCTOU patterns | 3 (blackjack, crash, coinflip) |
| Dead code blocks | 1 (O-1) |
| Recent git commits touching casino/rendering scope | 0 (last 5 commits were sportsbook/oracle/atlas_ai) |

---

*Priority order for fixes: C-1 (type mismatch — silent payout failure), C-2 (schema drift — silent data corruption), W-4 (assert in renderer), W-2 (permission bypass — admin inconsistency), W-7 (browser singleton lock — startup race).*

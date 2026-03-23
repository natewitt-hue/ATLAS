# ATLAS Nightly Review — 2026-03-23

**Auditor:** automated (`audit-monday-flow` scheduled task)
**Focus:** Flow & Economy subsystem — 10 files, 5-pass audit
**Bot version at audit time:** v6.12.0

---

## Commit Delta (last 24 h)

| Hash | Message | Files Touched |
|------|---------|---------------|
| `8900938` | fix: bug hunt sweep — race condition, token compliance, dead code removal — v6.12.0 | `economy_cog.py`, `flow_cards.py`, `flow_sportsbook.py`, `flow_wallet.py` |
| `23dce71` | feat(flow): overhaul Flow Hub stats, redesign My Bets, add auto-refresh — v6.11.0 | `economy_cog.py`, `flow_cards.py` |

**Net delta:** significant churn in `flow_cards.py` (My Bets redesign, Net P&L stat, results dot strip, streak badge) and `economy_cog.py` (auto-refresh loop added to `flow_cmd`). Both files are in audit scope — high priority for logic trace.

---

## Audit Findings

### 🔴 CRITICAL

#### C-01 — `flow_cards.py:168` — Closed connection accessed after `with` block

**Severity:** CRITICAL — crash for any user not yet in `users_table`
**File:** `flow_cards.py`, function `_get_leaderboard_rank()`

```python
def _get_leaderboard_rank(user_id: int) -> tuple[int, int]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT rank, total FROM ...").fetchone()
    if row:
        return row[0], row[1]
    # BUG: con is CLOSED here — the `with` block has exited
    total = con.execute("SELECT COUNT(*) FROM users_table").fetchone()[0]
    return total, total
```

The `with sqlite3.connect(...)` context manager calls `con.close()` on exit. Any user whose `user_id` is not yet ranked (new accounts, fresh season) hits the `if row` falsy branch and executes a query on a closed connection. Result: `sqlite3.ProgrammingError: Cannot operate on a closed database` — the entire hub card render fails for that user.

**Fix:** Open a second connection for the fallback query, or move both queries inside the `with` block:

```python
def _get_leaderboard_rank(user_id: int) -> tuple[int, int]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT rank, total FROM ...").fetchone()
        if row:
            return row[0], row[1]
        total = con.execute("SELECT COUNT(*) FROM users_table").fetchone()[0]
        return total, total
```

**Introduced by:** v6.11.0 My Bets redesign commit (`23dce71`) which added the rank display to the dashboard card.

---

#### C-02 — `flow_live_cog.py` (multiple lines) — Hardcoded DB filename ignores `FLOW_DB_PATH`

**Severity:** CRITICAL — silent data corruption / shadow database in non-default environments
**File:** `flow_live_cog.py`, functions: `_ensure_sessions_table`, `_persist`, `_delete_persisted`, `load_persisted`, `_ensure_state_table`, `_load_pulse_message_ids`, `_save_pulse_message_id`, `_get_jackpot_data`

Every `sqlite3.connect()` call in this file uses a raw string literal:

```python
conn = sqlite3.connect("flow_economy.db", timeout=10)
```

Every other module in the Flow subsystem resolves the path via `flow_wallet.DB_PATH`, which respects the `FLOW_DB_PATH` environment variable and resolves to an absolute path. `flow_live_cog.py` writes to a _relative path_ `flow_economy.db`, meaning it resolves against the current working directory at runtime — which may differ from where the authoritative database lives.

**Impact:** Session persistence, pulse message IDs, and jackpot data are silently written to a shadow file. The live dashboard shows stale/empty data, and on reboot the bot loses all persisted session state. Worse: if `FLOW_DB_PATH` is set to a non-default path, the live cog is entirely decoupled from the rest of the economy.

**Fix:** Import and use `DB_PATH` from `flow_wallet`:

```python
from flow_wallet import DB_PATH  # at top of file

# everywhere:
conn = sqlite3.connect(DB_PATH, timeout=10)
```

---

### 🟡 WARNING

#### W-01 — `flow_live_cog.py` — Blocking `sqlite3` I/O in async event handlers (no executor)

**File:** `flow_live_cog.py`, `SessionTracker._persist()` (L198), called at L278, L312
**Callers:** async `_on_game_result()` → `sessions.record()` → `_persist()` (sync, blocking)

`_persist()` opens a `sqlite3` connection and executes DML synchronously. It is called on every game result and sportsbook result event — potentially multiple times per second during peak play. Because there is no `asyncio.to_thread()` or `run_in_executor()` wrapper, this blocks the event loop for the duration of each disk write.

Contrast with `_get_jackpot_data()` (L608) in the same file, which is correctly wrapped:
```python
jp = await asyncio.get_running_loop().run_in_executor(None, _get_jackpot_data)
```

The inconsistency suggests `_persist()` was written before the `run_in_executor` pattern was established for this cog.

**Risk:** Under load (concurrent blackjack/slots sessions) the bot's heartbeat with Discord can stall, triggering reconnects. Low probability of user-visible failure in normal TSL traffic (~31 teams), but will become noticeable if casino usage grows.

**Fix:** Wrap in `asyncio.to_thread()`:
```python
async def _on_game_result(self, event):
    session = await asyncio.to_thread(self.sessions.record, event)
    ...
```
Or convert `_persist()` to async using `aiosqlite` (consistent with the rest of the economy stack).

---

#### W-02 — `economy_cog.py` + `flow_sportsbook.py` — Silent exception swallow after `defer()` leaves interaction dead

**File:** `economy_cog.py` `FlowHubView._swap_to()`, `flow_sportsbook.py` `FlowHubView._swap_to()`

```python
async def _swap_to(self, interaction, tab_cls, **kw):
    await interaction.response.defer()
    try:
        await self.render_current(interaction)
    except Exception:
        return  # ← interaction is now deferred with no followup
```

Once `defer()` is called, Discord expects either `edit_original_response()` or `followup.send()` within ~15 minutes. The bare `except Exception: return` swallows the error and returns without sending anything. Discord shows the user "application did not respond" after the deferred spinner. This affects every button press in both the Flow Hub and Sportsbook Hub.

**Fix:** At minimum, send an ephemeral error on failure:
```python
except Exception:
    log.exception("_swap_to failed")
    try:
        await interaction.followup.send("Something went wrong — try again.", ephemeral=True)
    except Exception:
        pass
```

---

#### W-03 — `flow_sportsbook.py` — `_check_parlay_completion` `own_con` path: `__enter__` without `__exit__` silently rolls back all changes

**File:** `flow_sportsbook.py`, `_check_parlay_completion()` (~L962)

```python
own_con = con is None
if own_con:
    con = _db_con().__enter__()  # acquires connection; __exit__ never called
try:
    # ... multi-statement DML (grade legs, credit balance, log payout) ...
finally:
    if own_con:
        con.close()  # closes connection WITHOUT __exit__ → rollback on close
```

`sqlite3` connections used as context managers commit on clean exit and rollback on exception. Calling `__enter__()` without the matching `__exit__()` means the connection manager state is never resolved. `con.close()` on a connection with uncommitted changes triggers an implicit rollback — silently discarding all the DML (parlay grading, balance credit, audit log).

**Current blast radius:** Zero — all three call sites pass `con=<existing connection>`, so `own_con` is always `False`. The path is dead code. But any future refactor that calls `_check_parlay_completion()` without a connection argument will cause silent data loss with no exception raised.

**Fix:** Use a proper context manager:
```python
if own_con:
    with _db_con() as con:
        _do_completion_work(con, ...)
        return
_do_completion_work(con, ...)
```
Or add an assertion: `assert con is not None, "must pass explicit connection"`.

---

### 🔵 OBSERVATIONS

#### O-01 — `flow_store.py:200,594` — `uuid4()` in `ref_key` breaks idempotency on transaction retry

```python
ref_key = f"store_purchase_{discord_id}_{item_id}_{uuid.uuid4().hex[:8]}"
ref_key = f"store_lootbox_{discord_id}_{inventory_id}_{uuid.uuid4().hex[:8]}"
```

The `transactions` table uses `reference_key` for idempotency (duplicate-spend protection). Every other subsystem (stipends, sportsbook) uses deterministic keys (`STIPEND_{id}_{member}_{period}`). Appending a random UUID suffix means each call generates a unique key — the idempotency check never fires. If the connection drops after `BEGIN IMMEDIATE` but before `COMMIT`, a retry would create a second transaction row.

In practice the risk is low because the per-user `asyncio.Lock` prevents concurrent access, and the `is_activated=1` guard on the inventory row prevents double-open. But the idempotency table is providing false confidence here.

**Suggestion:** Use a deterministic key: `f"store_purchase_{discord_id}_{item_id}_{timestamp_bucket}"` or `f"store_lootbox_{discord_id}_{inventory_id}"`.

---

#### O-02 — `flow_live_cog.py:648-649` — Pulse dashboard hardcodes sportsbook/prediction metrics to zero

```python
sb_week=0, sb_bets=0, sb_volume=0, sb_hot_player=None, sb_hot_desc="",
pred_open=0, pred_hot_title="", pred_yes_pct=0, pred_no_pct=0, pred_volume=0,
```

The Pulse dashboard card has sportsbook and prediction sections, but the data builder is passed hard-coded zeros. The rendered card shows empty/zero stats for sportsbook week and prediction markets. This appears to be intentional scaffolding (the section exists in the renderer), but worth noting — users see a dashboard that implies live sportsbook data is being tracked when it isn't.

---

#### O-03 — `flow_live_cog.py` — `_get_jackpot_data` uses `run_in_executor` correctly (positive reference)

```python
jp = await asyncio.get_running_loop().run_in_executor(None, _get_jackpot_data)
```

This is the right pattern for sync SQLite in an async context. It's also the correct precedent to apply to `_persist()` (W-01 above).

---

## Cross-Module Risk Map

| Risk | Modules Involved | Trigger Condition |
|------|-----------------|-------------------|
| New user hits Flow Hub → render crash (C-01) | `flow_cards.py` ← `economy_cog.py` | User with no rank in `users_table` opens hub |
| Live dashboard writes to wrong DB (C-02) | `flow_live_cog.py` ↔ `flow_wallet.py` | Any non-default `FLOW_DB_PATH` deployment or CWD mismatch |
| Event loop stall during peak casino play (W-01) | `flow_live_cog.py` ← `flow_events.py` (bus) ← `casino/casino.py` | Multiple concurrent game results firing at once |
| Hub button press → spinner with no response (W-02) | `economy_cog.py`, `flow_sportsbook.py` → Discord API | Any exception in `render_current()` after defer |
| Silent parlay data loss if `own_con` path ever triggered (W-03) | `flow_sportsbook.py` | Calling `_check_parlay_completion()` without `con=` argument |

---

## Positive Patterns (worth preserving)

| Pattern | Where | Why It's Good |
|---------|-------|---------------|
| `get_user_lock(discord_id)` + `BEGIN IMMEDIATE` | `flow_wallet.py`, `flow_store.py` | Per-user lock prevents concurrent double-spend; IMMEDIATE acquires write lock upfront, eliminating TOCTOU in balance reads |
| Deterministic `ref_key` idempotency | `flow_wallet.py`, `economy_cog.py` stipends | Prevents duplicate credits even if the caller retries |
| `@functools.lru_cache` + cache invalidation in setter | `flow_wallet.get_theme_for_render()` | Theme lookups are hot path (every render); invalidation on `set_theme()` keeps it correct |
| WAL mode on every connection | `flow_store.py`, `flow_wallet.py` | Concurrent readers don't block writers; critical for Discord's concurrent interaction model |
| `FlowEventBus` exception isolation | `flow_events.py` | Exception in one subscriber doesn't kill other subscribers — live cog failure won't break casino payout |
| `AuditResult` dataclass + exception-safe checks | `flow_audit.py` | Each audit check is independently isolated; a bad check surfaces as a HIGH finding rather than crashing the whole audit |

---

## Test Gaps

| Gap | Risk Level | Trigger |
|-----|-----------|---------|
| `_get_leaderboard_rank()` fallback path (new user not in `users_table`) | CRITICAL | First hub open for any new user |
| `_check_parlay_completion()` with `con=None` (own_con path) | HIGH | Future refactor removes `con=` argument from callers |
| `flow_live_cog` with `FLOW_DB_PATH` set to non-default path | HIGH | VPS deployment with custom DB location |
| `_swap_to()` when `render_current()` raises | MEDIUM | Any card render failure during tab navigation |
| Lootbox `guaranteed_rarity` fallback (10 retries exhausted, take max rarity) | LOW | Extremely low-weight rarity pool |
| Store purchase / lootbox retry after mid-transaction disconnect | LOW | Network blip during `BEGIN IMMEDIATE` |

---

## Metrics

| Metric | Value |
|--------|-------|
| Files audited | 10 |
| Lines of code reviewed | ~5,900 (estimated across all 10 files) |
| Critical findings | 2 |
| Warning findings | 3 |
| Observations | 3 |
| SQL injection vectors found | 0 (1 apparent f-string in `flow_sportsbook._set_line_override` is whitelist-validated against `_ALLOWED_LINE_COLS`) |
| Bare `except:` clauses | 0 |
| `time.sleep()` in async context | 0 |
| Blocking SQLite in async (without executor) | 1 (`flow_live_cog._persist`) |
| Hardcoded DB paths | 8 call sites in `flow_live_cog.py` |

---

*Generated by ATLAS audit-monday-flow scheduled task. All findings are code-level — no production incidents observed. Priority order for fixes: C-01 (any new user reproduces), C-02 (environment-dependent), W-02 (UX regression on any render error), W-01 (performance at scale), W-03 (pre-emptive before refactor).*

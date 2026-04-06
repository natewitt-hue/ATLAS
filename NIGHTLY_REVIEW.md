# ATLAS Nightly Audit — 2026-04-06

**Focus:** Flow & Economy Module | **Recent commits (24h):** 0 | **Files audited:** 14 | **Lines reviewed:** ~9,000+

**Modules covered:** `sportsbook_core.py`, `flow_sportsbook.py`, `economy_cog.py`, `flow_wallet.py`, `real_sportsbook_cog.py`, `wager_registry.py`, `flow_events.py`, `espn_odds.py`, `polymarket_cog.py`, `flow_store.py`, `flow_audit.py`, `flow_live_cog.py`, `flow_cards.py`, `sportsbook_cards.py`

---

## CRITICAL

### [CRIT-1] `_place_real_bet()` debits wallet without `reference_key` — double-debit risk
**File:** `real_sportsbook_cog.py` L778
**Confidence:** HIGH

```python
new_balance = await flow_wallet.debit(
    uid, amt, "REAL_BET",
    description=f"Bet: {pick} ({bet_type})",
    # ← NO reference_key
)
```

The debit has no idempotency key. If the interaction fires twice (button double-click, Discord retry, network timeout + retry), the wallet is debited twice. The straight-bet path in `flow_sportsbook.py` correctly uses `debit_ref = f"TSL_BET_DEBIT_{uid}_{game_id}_{int(time.time())}"`. The refund-on-failure path in the same function *does* use a `reference_key` (L813), making the asymmetry more visible.

**Fix:** Add `reference_key=f"REAL_BET_DEBIT_{uid}_{espn_event_id}_{int(time.time())}"` to the `debit()` call at L778. Using `espn_event_id` as the primary key component (not `game_id`) prevents cross-bet collision while still bounding the idempotency window.

---

## WARNINGS

### [WARN-1] Parlay debit lacks `reference_key` — retry risk
**File:** `flow_sportsbook.py`, `ParlayWagerModal.on_submit()` ~L1353
**Confidence:** MEDIUM

`update_balance_sync(..., subsystem="PARLAY")` is called without a `reference_key`. Unlike straight bets (which use `debit_ref = f"TSL_BET_DEBIT_..."`), parlay debits have no idempotency guard. Modal `on_submit()` is called by Discord once per interaction, but Discord can replay interactions on timeout — same double-debit exposure as CRIT-1.

**Fix:** Generate `ref = f"TSL_PARLAY_DEBIT_{uid}_{int(time.time())}"` before the debit call and pass it as `reference_key`.

---

### [WARN-2] N+1 aiosqlite connection per pending bet in `settle_event()`
**File:** `sportsbook_core.py` ~L270
**Confidence:** HIGH

The outer `settle_event()` loop opens a new `aiosqlite.connect(DB_PATH)` for each pending bet (`async with aiosqlite.connect(...) as con:`). Under WAL mode this is safe for correctness, but it wastes connection overhead on every settlement batch. If 10 bets settle simultaneously, 10 connections open and close.

**Fix:** Hoist the connection outside the loop and pass it into the bet-grading path, or collect all bets first and open a single connection for the batch.

---

### [WARN-3] Silent bare `except: pass` in admin bet views — errors invisible
**File:** `flow_sportsbook.py` L3477-3478 (ESPN bets admin view), L3536-3537 (settled bets admin view)
**Confidence:** HIGH

```python
except Exception:
    pass  # ← swallows all DB errors silently
```

Both admin-facing views return empty results on any DB error with no log entry. If the `real_bets` table has a schema issue or DB is locked, the admin sees a blank list with no indication anything went wrong.

**Fix:** Replace `pass` with `log.exception("ESPN bets admin view failed")` / `log.exception("Settled bets admin view failed")`. Optionally surface an error embed instead of empty results.

---

### [WARN-4] `sportsbook_cards._get_season_start_balance` missing `OperationalError` guard
**File:** `sportsbook_cards.py` L87-94
**Confidence:** MEDIUM

```python
def _get_season_start_balance(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT season_start_balance FROM users_table WHERE discord_id = ?",
            (user_id,)
        ).fetchone()
    return row[0] if row else STARTING_BALANCE
    # ← no try/except for OperationalError
```

The equivalent function in `flow_cards.py` (L44-53) wraps the entire query in `try/except sqlite3.OperationalError` with a safe fallback comment: `# column may not exist on older DBs`. `sportsbook_cards.py` omits this guard. On a DB that predates the `season_start_balance` column migration, this raises an unhandled exception and crashes `build_sportsbook_card()` / `build_stats_card()`.

**Fix:** Mirror the `flow_cards.py` pattern — wrap in `try/except sqlite3.OperationalError: return 0`.

---

## OBSERVATIONS

### [OBS-1] `"sportsbook_result"` is a dead event topic — no publisher exists
**File:** `flow_live_cog.py` L428, `flow_sportsbook.py`, `real_sportsbook_cog.py`

`FlowLiveCog.__init__` subscribes to `flow_bus.subscribe("sportsbook_result", ...)`. No code anywhere publishes to `"sportsbook_result"`. Settlement goes through `EVENT_FINALIZED = "event_finalized"` (consumed by `sportsbook_core`), but `sportsbook_core` doesn't re-emit a `"sportsbook_result"` event for `flow_live_cog`.

**Effect:** Sportsbook wins/losses never register as live sessions. Parlay highlights never fire. `_on_sportsbook_result()` and `record_sportsbook()` are effectively dead code paths. The pulse dashboard sportsbook section stays zeroed.

**Fix:** After settlement, emit `SportsbookEvent` on `"sportsbook_result"` — either from `sportsbook_core.settle_event()` after each credit, or from `flow_sportsbook.py` / `real_sportsbook_cog.py` after grading. Requires passing `guild_id` into the settlement path.

---

### [OBS-2] Pulse dashboard sportsbook stats hardcoded to zeros
**File:** `flow_live_cog.py` L648-649

```python
sb_week=0, sb_bets=0, sb_volume=0, sb_hot_player=None, sb_hot_desc="",
```

These are always `0` / `None`. No DB query fetches live sportsbook data for the pulse card. Related to OBS-1 — even if events were published, the pulse aggregation doesn't query sportsbook tables.

---

### [OBS-3] `record_sportsbook()` doesn't append to session `events` list
**File:** `flow_live_cog.py` L282-313

`SessionTracker.record_sportsbook()` updates session counters (`wins`, `losses`, `net_profit`, etc.) but never calls `session.events.append(...)`. Session recap cards (`render_session_recap`) query `session.events`, so sportsbook results are excluded from the recap event history even if OBS-1 were fixed.

---

### [OBS-4] `_get_leaderboard_rank` in `sportsbook_cards.py` uses linear scan vs SQL window function
**File:** `sportsbook_cards.py` L199-213

Fetches all rows ordered by balance, then iterates in Python to find rank. The code comment acknowledges this is intentional for ~31 owners. `flow_cards.py` uses `RANK() OVER (ORDER BY balance DESC)` for O(1) lookup. Worth noting the inconsistency — if user count scales, the linear scan degrades. Low priority.

---

## CROSS-MODULE RISKS

### [XMOD-1] Real bet write split creates settlement vs. admin-read divergence
**Files:** `real_sportsbook_cog.py` L790-818, `flow_cards.py` `_gather_my_bets_data()`

`_place_real_bet()` writes to two places: `sportsbook_core.write_bet()` (flow.db) for settlement and a legacy `real_bets` table (flow_economy.db) for admin card reads. These are separate `try/except` blocks. If `write_bet()` succeeds but the legacy insert fails (or vice versa), the two DBs are out of sync: the bet settles but doesn't appear in the user's My Bets card, or vice versa. No reconciliation exists.

**Current mitigation:** The refund path fires if `write_bet()` itself raises, but not if the legacy insert fails silently.

---

### [XMOD-2] `sportsbook_core.finalize_event()` re-raises on missing event row
**Files:** `sportsbook_core.py` `finalize_event()`, `real_sportsbook_cog.py` `_sync_scores()` L606-615

`finalize_event()` raises `ValueError` if the event row doesn't exist (requires `write_event()` to be called first). In `_sync_scores()`, `write_event()` is called before `finalize_event()` — but if `write_event()` raises (e.g., DB locked), `finalize_event()` is never reached, and the score is silently not settled. The outer `except Exception: log.exception(...)` catches it, but there's no retry or deferred queue for missed finalizations. The 10-minute `settlement_poll()` fallback in `sportsbook_core` catches stragglers, but only for events already written to flow.db.

---

## POSITIVE PATTERNS WORTH PRESERVING

| Pattern | Location | Why It Works |
|---------|-----------|--------------|
| Credit-first two-phase settlement | `sportsbook_core.settle_event()` | Wallet credit always succeeds before status update; `BEGIN IMMEDIATE` prevents double-credit |
| Per-user asyncio locks | `flow_wallet.get_user_lock()` | Serializes concurrent requests at the Python level before hitting SQLite |
| `VALID_TRANSITIONS` state machine | `wager_registry.py` | Enforces legal state changes at the registry layer, not at each call site |
| Parlay legs batch-fetch (IN clause) | `flow_cards._gather_my_bets_data()` L593-604 | Single query for all parlay legs vs N+1 queries per parlay |
| Stipend idempotency (mark before loop) | `economy_cog.pay_stipends()` | `mark_stipend_paid()` called before the payout loop — crash can't cause double-pay |
| ESPN rate-limit + TTL cache + backoff | `espn_odds.py` | 0.3s inter-request floor, per-endpoint TTL caches, exponential 429 backoff |
| `OperationalError` guard on column reads | `flow_cards._get_season_start_balance()` | Safe fallback for DB migrations that haven't run yet |
| Pulse message edit-in-place | `flow_live_cog._update_pulse()` | Fetches existing msg_id, edits vs deletes/re-posts; avoids channel noise on 60s tick |

---

## TEST GAPS

| Gap | Risk | Suggested Test |
|-----|------|----------------|
| `_place_real_bet()` double-invocation | CRIT-1 — double debit | Concurrent call test: two async tasks call `_place_real_bet()` for same user; assert wallet debited once |
| `settle_event()` idempotency | Re-settlement could double-credit | Call `settle_event()` twice with same event_id; assert second call is a no-op |
| `wager_registry` invalid transition rejection | Silent bugs if status machine bypassed | Assert `update_wager_status("won" → "open")` raises `ValueError` |
| `flow_live_cog` session restore cycle | Persist + reconnect + replay | Persist a session to DB, call `load_persisted()`, assert session is restored and event count preserved |
| `"sportsbook_result"` topic coverage | Dead subscription (OBS-1) undetected | Integration test: confirm a sportsbook settlement triggers a session update in `SessionTracker` |

---

## METRICS

| Metric | Value |
|--------|-------|
| Files audited | 14 |
| Lines of code reviewed | ~9,000 |
| Git commits (last 24h) | 0 |
| CRITICAL findings | 1 |
| WARNINGS | 4 |
| OBSERVATIONS | 4 |
| CROSS-MODULE RISKS | 2 |
| POSITIVE patterns noted | 8 |
| TEST gaps identified | 5 |
| Files with silent `except: pass` | 2 (`flow_sportsbook.py` ×2 sites) |
| Dead event subscriptions | 1 (`"sportsbook_result"` — flow_live_cog) |

---

## CLAUDE.md UPDATES

### Health Check Results

**Module Map** — all 14 audited files are represented in the Module Map. No gaps found.

**Cog Load Order** — `flow_live_cog` (position 13) appears correctly after `flow_store` (12). Verified against `_EXTENSIONS` in `bot.py`. No ordering issues found.

**New rule candidate (from WARN-3 pattern):**

> **Admin view exception handling** — Silent `except Exception: pass` in admin-facing views is prohibited. Always log the exception; optionally surface an error embed. Admin views silently returning empty results on DB error are worse than a visible error.

**New gotcha candidate (from CRIT-1 pattern):**

| Rule | Detail |
|------|--------|
| `flow_wallet.debit()` idempotency | All debit calls MUST pass a `reference_key`. Without it, Discord interaction retries or button double-clicks cause double-debits. Use `f"{SUBSYSTEM}_DEBIT_{uid}_{event_id}_{int(time.time())}"` as the key format. |

**New observation (from OBS-1):**

> `"sportsbook_result"` event topic — `flow_live_cog` subscribes but no live code publishes. The parlay highlight and sportsbook session tracking code paths in `flow_live_cog` are entirely inactive until a publisher is wired into `sportsbook_core.settle_event()` or the grading paths.

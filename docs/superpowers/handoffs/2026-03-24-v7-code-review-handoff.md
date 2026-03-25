# Handoff: ATLAS v7 Unified Event-Bet Architecture — Code Review

**Branch:** main
**Commits reviewed:** `bf43d21` → `44092d3` (12 commits)
**Spec:** `docs/superpowers/specs/2026-03-24-unified-event-bet-architecture-design.md`
**Prior handoff (context):** `docs/superpowers/handoffs/2026-03-24-unified-event-bet-handoff.md`

---

## What This Session Did

Implemented the complete ATLAS Unified Event-Bet Architecture (v7). Three independent bet-grading pipelines (TSL/Madden, real sports, prediction markets) were replaced by a single `sportsbook_core.py` module backed by a new `flow.db` database. Every bet is now linked to a pre-written `events` table row by foreign key. One settlement worker (`settle_event`) grades everything via a credit-first two-DB pattern.

**New file:** `sportsbook_core.py` (~350 lines)
**Modified files:** `flow_sportsbook.py`, `real_sportsbook_cog.py`, `polymarket_cog.py`, `flow_events.py`, `bot.py`, `boss_cog.py`
**New tests:** `tests/test_sportsbook_core.py` (20 tests, all passing)

---

## Your Job: Full Code Review

Read the new code thoroughly and look for bugs, logic errors, edge cases, and anything that could cause silent failures or data loss in production.

### Files to read in this order

1. **`sportsbook_core.py`** — read every line. This is the most critical file.
2. **`tests/test_sportsbook_core.py`** — check test coverage gaps
3. **`flow_sportsbook.py`** — focus on `_write_tsl_events()`, `_place_straight_bet()`, `ParlayWagerModal.on_submit()`
4. **`real_sportsbook_cog.py`** — focus on `_place_real_bet()` and `_sync_scores()`
5. **`polymarket_cog.py`** — focus on `_execute_prediction_buy()`, `_finalize_resolved_pass()`, `_resolve_market_impl()`
6. **`flow_events.py`** — just verify `EVENT_FINALIZED` constant was added correctly
7. **`bot.py`** — verify `sportsbook_core` wiring in `setup_hook()`

---

## Known Issues / Things to Scrutinize

These were flagged during implementation but accepted as-is. Verify each one is actually safe:

### 1. `execute_fetchone` vs cursor pattern
`sportsbook_core.py` uses `cur = await db.execute(...); row = await cur.fetchone()` throughout. Verify this is correct for aiosqlite 0.22.x (the version installed). Confirm `row["column_name"]` dict access works correctly with `db.row_factory = aiosqlite.Row`.

### 2. `_settle_locks` persists across calls — inter-test state
`_settle_locks: dict[str, asyncio.Lock] = {}` is a module-level global. In tests, it's cleared manually. In production, locks accumulate in memory for every event ever settled. This is fine (low memory), but verify there's no case where a lock is held indefinitely (e.g., if `settle_event` raises an unhandled exception inside the `async with lock:` block — the context manager releases the lock, so this should be fine).

### 3. event_id consistency: TSL
`_write_tsl_events()` in `flow_sportsbook.py` builds `event_id` as:
- Primary: `f"tsl:{scheduleId}"` if `scheduleId` is present and not `"0"`
- Fallback: `f"tsl:s{season}:w{week}:{homeTeamId}v{awayTeamId}"`

`_place_straight_bet()` builds event_id as `f"tsl:{game_id}"` independently (not using the stored-back `g["event_id"]`).

**Verify:** Does `game_id` in the game dict from `_build_game_lines()` equal `scheduleId` from the MaddenStats API? If they differ, `write_event` writes `tsl:{scheduleId}` but `write_bet` tries to FK-reference `tsl:{game_id}` — which won't exist. This would cause a FK violation on every bet placement.

### 4. Parlay placement in flow_sportsbook.py — non-atomic two-DB write
`ParlayWagerModal.on_submit()` writes the parlay to the old DB (atomically) and then mirrors it to `flow.db` via `sportsbook_core`. The mirror is in a `try/except` that silently swallows failures. If the mirror fails, the user has a parlay in the old DB but nothing in `flow.db` — meaning `settle_event()` will never find any Pending bets for it and the parlay will never settle.

**Verify:** Is there a recovery path for this case? Does `settlement_poll` catch it?

### 5. `settlement_poll` starts unconditionally
In `bot.py`, `sportsbook_core.settlement_poll.start()` is called at the end of `setup_hook()`. If `flow.db` doesn't exist yet (first boot before migration creates it), the poll will error immediately. Check: does `setup_db()` run before this? (It should — it's called at the top of `setup_hook()`.)

### 6. `prediction_contracts` display queries post-migration
`polymarket_cog.py` still has read queries against `prediction_contracts` for portfolio display, open position views, etc. After `run_migration_v7()` renames it to `prediction_contracts_arc_v7`, these reads will return empty results. No crash, but users will see empty portfolio. Confirm this is acceptable or needs a follow-up.

### 7. `_migrate_contracts_sold_status()` guard
A `sqlite_master` guard was added to `polymarket_cog.py` to skip the migration probe if `prediction_contracts` doesn't exist. Verify this doesn't mask a real error if the table is absent for an unexpected reason.

### 8. Boss cog manual grade command
`boss_cog.py` was modified so `BossSBGradeModal.on_submit` now returns `"Manual grading is no longer supported — settlement is automatic via the event bus."` Verify this is the right UX and won't confuse commissioner workflows.

---

## What to Verify Against Spec

The original spec had a 12-point verification checklist. These are the ones hardest to verify via code review alone (flag them for runtime testing):

| # | Check | How to verify in code |
|---|-------|-----------------------|
| V5 | FK enforcement: `bets.event_id` must reference valid `events` row | Confirm `PRAGMA foreign_keys=ON` is set on EVERY `aiosqlite.connect(FLOW_DB)` call in `sportsbook_core.py` — count them all |
| V8 | Two-DB crash safety | Trace the exact order in `settle_event()`: wallet credit → re-check Pending → UPDATE. Confirm `reference_key` format is `f"BET_{bet_id}_settled"` everywhere |
| V11 | Idempotency | Trace `settle_event()` second-call path: lock check → event fetch → pending fetch returns [] → early return. Correct? |
| V12 | Concurrent settlement | The `if lock.locked(): return` guard — does this correctly prevent a second `settle_event(same_id)` call that arrives while the first is mid-execution? Or is there a TOCTOU race between `lock.locked()` check and `async with lock`? |

**Note on V12:** The `if lock.locked(): return` followed by `async with lock:` has a subtle race: two coroutines could both check `lock.locked()` as False, then both proceed to `async with lock:`. The second one would block until the first releases, then execute again. The Pending re-check inside the lock prevents double-grading, but it's worth verifying this double-entry path doesn't cause any other issue.

---

## Suggested Opening Prompt for Review Session

```
I need you to do a thorough code review of the ATLAS v7 Unified Event-Bet Architecture implementation.

The handoff doc is at:
  docs/superpowers/handoffs/2026-03-24-v7-code-review-handoff.md

The original spec is at:
  docs/superpowers/specs/2026-03-24-unified-event-bet-architecture-design.md

Read the handoff doc first (it lists exactly which files to read and what to scrutinize), then read every file listed in order. Look for bugs, logic errors, silent failure modes, and data-loss risks.

Pay special attention to:
1. The TSL event_id consistency issue (Issue #3 in the handoff) — this could be a bet-blocking bug
2. The concurrent settlement TOCTOU race (Issue V12) — correctness of the asyncio lock pattern
3. PRAGMA foreign_keys=ON coverage — count every aiosqlite.connect() in sportsbook_core.py and verify each sets it
4. The parlay mirror failure path (Issue #4)

For each issue found, provide: the file, the line number, what the bug is, and a concrete fix.
```

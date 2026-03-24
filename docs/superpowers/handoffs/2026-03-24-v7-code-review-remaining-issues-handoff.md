# Handoff: ATLAS v7 Code Review — Remaining Important Issues

**Branch:** main
**Session prior work:** 4 fixes applied (uncommitted) to `sportsbook_core.py`, `real_sportsbook_cog.py`, `polymarket_cog.py`, `flow_sportsbook.py`
**Spec:** `docs/superpowers/specs/2026-03-24-unified-event-bet-architecture-design.md`
**Prior review handoff:** `docs/superpowers/handoffs/2026-03-24-v7-code-review-handoff.md`

---

## What Was Already Fixed (Do Not Re-Do)

These Critical issues were resolved in the previous session (changes are uncommitted and sitting in the working tree):

| Fix | File | What changed |
|-----|------|-------------|
| C1 | `real_sportsbook_cog.py` | Wrapped `write_event`/`write_bet` in try/except with refund path |
| C2 | `polymarket_cog.py` | Restored `INSERT INTO prediction_contracts` dual-write; `contract_id` is now `prediction_contracts.id` again |
| C3 | `sportsbook_core.py` | `finalize_event()` raises `ValueError` if UPDATE rowcount=0 |
| I2 | `flow_sportsbook.py` | Removed dead `scheduleId` branch from `_write_tsl_events` |

All 20 tests in `tests/test_sportsbook_core.py` pass with these changes.

---

## Your Job: Fix the 4 Remaining Important Issues

### I1 — Parlay mirror failure leaves parlays orphaned with no recovery path

**File:** `flow_sportsbook.py`, `ParlayWagerModal.on_submit()` (~line 1338)

**Problem:** After the old-DB write succeeds (atomic, line ~1296), the flow.db mirror block (lines 1339–1368) is wrapped in a bare `except Exception` that only logs. If any `write_bet` call fails mid-loop — e.g. because an event row doesn't exist in flow.db yet for one leg, or flow.db is locked — then:
- The parlay header row (`write_parlay`) may or may not exist
- Some legs are written, some aren't
- `settle_event` will never find all Pending bets → parlay never settles
- There is no admin command to re-mirror or reconcile

**Fix approach:**
1. Wrap the entire mirror block in a transaction: call `write_parlay` first, then all legs in a loop. If ANY leg fails, catch the exception and attempt to clean up (delete the partial parlay/bet rows from flow.db) so the state is clean — either fully mirrored or not at all.
2. Add a structured `log.error` (not just `log.exception`) on mirror failure with enough data for manual reconciliation: `parlay_id`, `discord_id`, `amt`, `len(legs)`.

**Constraints:**
- The old-DB write (the primary record) must not be rolled back — the user's funds are already debited and the parlay is in the old DB. Only the flow.db mirror needs cleanup.
- `sportsbook_core.write_parlay`, `write_bet`, `write_parlay_leg` are all separate async functions with separate connections. A rollback must explicitly delete the rows written so far.

---

### I3 — `_check_parlay_completion` credit and status update are not atomic

**File:** `sportsbook_core.py`, `_check_parlay_completion()` (~line 143)

**Problem:** The function:
1. Reads leg statuses (lines 153–167) — first DB connection, closes
2. Evaluates outcome
3. Calls `flow_wallet.credit(...)` at line 189 — outside any lock
4. Opens a second `BEGIN IMMEDIATE` connection to re-check and update status (lines 194–206)

Between steps 3 and 4, another coroutine settling a different leg of the same parlay could also call `_check_parlay_completion(same_parlay_id)`. Both pass the "all legs resolved" check, both call `flow_wallet.credit` — the `reference_key=f"PARLAY_{parlay_id}_settled"` deduplication prevents double-payment, but both callers then proceed to the `BEGIN IMMEDIATE` re-check. The second one hits `status != 'Pending'` and rolls back, which is correct. However, the credit and the status update are not in the same atomic unit — a crash between steps 3 and 4 leaves the user credited but the parlay row still `Pending`, causing `settlement_poll` to re-credit on next tick (blocked by reference_key, but the parlay stays Pending forever).

**Fix approach:**
Check if `flow_wallet.credit` supports a `con=` parameter (look at `flow_wallet.py`). If yes, move the credit call inside the `BEGIN IMMEDIATE` block so credit + status update are atomic. If `flow_wallet.credit` doesn't support `con=`, the minimum fix is to move the status update to happen BEFORE the credit (reverse the two-DB order used in `settle_event`) — this way, if the process crashes after status update but before credit, `settlement_poll` re-runs and `flow_wallet.credit` is idempotent via reference_key.

**Note:** The existing `settle_event` function already handles this correctly with credit-first + re-check. The parlay completion function should use the same pattern consistently.

---

### I4 — `_migrate_contracts_sold_status` guard silently swallows unexpected table absence

**File:** `polymarket_cog.py`, `_migrate_contracts_sold_status()` (~line 558)

**Problem:** Lines 564–569 return silently if `prediction_contracts` doesn't exist (expected post-v7 migration). But if the table is absent for an unexpected reason (manual DROP, corruption), the function returns silently — no log entry, no warning. Future schema probes would be skipped, and if a re-created table came from a stale schema, data integrity issues would follow.

**Fix:** Add a `log.warning` when the table is absent, so the absence is visible in logs even when expected:

```python
if not await _cur.fetchone():
    log.warning("[POLY] _migrate_contracts_sold_status: prediction_contracts not found — skipping (expected post-v7 migration)")
    return
```

This is a one-line change.

---

### I5 — Test coverage gaps in `tests/test_sportsbook_core.py`

**File:** `tests/test_sportsbook_core.py`

**Problem:** The test suite has 20 tests covering `grade_bet` branches and basic `settle_event` idempotency. Missing:
- No test for a single **Won** bet through `settle_event` asserting wallet was credited
- No test for a single **Lost** bet asserting no credit and status=`Lost`
- No test for a **Push** bet asserting wager refunded
- No test that a parlay leg's `_check_parlay_completion` is triggered from `settle_event` (the whole two-stage settlement)

**Fix:** Add 4 new tests. The existing `test_settle_event_idempotent` seeds a Won bet and validates idempotency — use it as a template. You'll need to mock `flow_wallet.credit` (already mocked in the existing tests) and assert on call args.

Look at how the existing `test_settle_event_idempotent` test sets up its fixtures (seeds event + bet rows, mocks `flow_wallet.credit`, calls `settle_event`) — follow that pattern exactly for the new tests.

---

## Files to Read

In this order:
1. `tests/test_sportsbook_core.py` — understand test setup patterns before writing new tests
2. `sportsbook_core.py` (~lines 143–210) — `_check_parlay_completion` for I3
3. `flow_wallet.py` — check if `credit()` accepts a `con=` parameter (needed for I3 fix decision)
4. `flow_sportsbook.py` (~lines 1338–1398) — `ParlayWagerModal.on_submit` for I1
5. `polymarket_cog.py` (~lines 558–570) — `_migrate_contracts_sold_status` for I4

---

## Verification

After all fixes:

```bash
python -m pytest tests/test_sportsbook_core.py -v
python -m pytest tests/ -v
```

Confirm:
- All 20 original tests still pass
- 4 new settlement tests pass (I5)
- No import errors

---

## Suggested Opening Prompt

```
I need you to fix 4 remaining Important issues from the ATLAS v7 code review.

The handoff is at:
  docs/superpowers/handoffs/2026-03-24-v7-code-review-remaining-issues-handoff.md

Read the handoff first — it describes each issue, the fix approach, and which files to read.

Note: 4 Critical issues were already fixed in the prior session (uncommitted changes in the working tree). Do NOT re-do those. Only fix I1, I3, I4, I5.

Start by reading tests/test_sportsbook_core.py and flow_wallet.py before touching any code.
```

# GAP 6: Jackpot Payouts Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag casino jackpot credit transactions with subsystem metadata, record jackpot payouts in the house bank ledger, and backfill historical untagged jackpot transactions.

**Architecture:** Three surgical fixes in `casino/casino_db.py`: (1) plumb `session_id` through the jackpot call chain and add `subsystem`/`subsystem_id` to `flow_wallet.credit()`, (2) INSERT a `jackpot_payout` row into `casino_house_bank`, (3) a startup backfill function that tags historical NULL-subsystem jackpot transactions via correlated subquery with timestamp proximity. Version bump in `bot.py`.

**Tech Stack:** Python 3.14, aiosqlite, SQLite

**Spec:** `docs/superpowers/specs/2026-03-20-gap6-jackpot-payouts-audit-design.md`

---

## File Map

| File | Change |
|------|--------|
| `casino/casino_db.py` | Modify `_contribute_and_check_jackpot()` and `_award_jackpot()` signatures; add subsystem tags + house_bank entry; add `backfill_jackpot_tags()` |
| `bot.py` | Add backfill call at startup; version bump 4.6.0 → 4.7.0 |

---

### Task 1: Plumb `session_id` Through Jackpot Call Chain

**Files:**
- Modify: `casino/casino_db.py:427-428` (signature of `_contribute_and_check_jackpot`)
- Modify: `casino/casino_db.py:475` (call to `_award_jackpot` inside `_contribute_and_check_jackpot`)
- Modify: `casino/casino_db.py:480` (signature of `_award_jackpot`)
- Modify: `casino/casino_db.py:931-935` (call site in `process_wager`)

- [ ] **Step 1: Update `_contribute_and_check_jackpot` signature**

At `casino/casino_db.py:427-428`, add `session_id: int` parameter:

```python
async def _contribute_and_check_jackpot(
    discord_id: int, wager: int, game_type: str, streak_len: int, con, session_id: int
) -> dict | None:
```

- [ ] **Step 2: Pass `session_id` to `_award_jackpot`**

At `casino/casino_db.py:475`, change:
```python
            return await _award_jackpot(tier, discord_id, game_type, con)
```
to:
```python
            return await _award_jackpot(tier, discord_id, game_type, con, session_id)
```

- [ ] **Step 3: Update `_award_jackpot` signature**

At `casino/casino_db.py:480`, change:
```python
async def _award_jackpot(tier: str, discord_id: int, game_type: str, con) -> dict:
```
to:
```python
async def _award_jackpot(tier: str, discord_id: int, game_type: str, con, session_id: int) -> dict:
```

- [ ] **Step 4: Update call site in `process_wager`**

At `casino/casino_db.py:933-934`, change:
```python
                jackpot_result = await _contribute_and_check_jackpot(
                    discord_id, wager, game_type, streak_info.get("len", 0), db
                )
```
to:
```python
                jackpot_result = await _contribute_and_check_jackpot(
                    discord_id, wager, game_type, streak_info.get("len", 0), db, session_id
                )
```

- [ ] **Step 5: Commit**

```bash
git add casino/casino_db.py
git commit -m "refactor(casino): plumb session_id through jackpot call chain (GAP 6 prep)"
```

---

### Task 2: Tag Jackpot Credit With Subsystem Metadata

**Files:**
- Modify: `casino/casino_db.py:496-501` (the `flow_wallet.credit()` call inside `_award_jackpot`)

- [ ] **Step 1: Add subsystem tags to credit call**

At `casino/casino_db.py:496-501`, change:
```python
    await flow_wallet.credit(
        discord_id, amount, "CASINO",
        description=f"JACKPOT {tier.upper()} win!",
        reference_key=ref_key,
        con=con,
    )
```
to:
```python
    await flow_wallet.credit(
        discord_id, amount, "CASINO",
        description=f"JACKPOT {tier.upper()} win!",
        reference_key=ref_key,
        subsystem="CASINO",
        subsystem_id=str(session_id),
        con=con,
    )
```

- [ ] **Step 2: Commit**

```bash
git add casino/casino_db.py
git commit -m "fix(casino): tag jackpot credits with subsystem metadata (GAP 6 fix 1)"
```

---

### Task 3: Add House Bank Jackpot Payout Entry

**Files:**
- Modify: `casino/casino_db.py:509` (after the `UPDATE casino_jackpot` block inside `_award_jackpot`, before the log INSERT)

- [ ] **Step 1: Add house_bank INSERT**

After the `UPDATE casino_jackpot SET pool = ...` block (line 509) and before the jackpot log INSERT (line 512), add:

```python
    # Record jackpot payout in house bank (money leaving house to player)
    await con.execute(
        "INSERT INTO casino_house_bank (game_type, delta, session_id, recorded_at) VALUES (?,?,?,?)",
        ("jackpot_payout", -amount, session_id, now),
    )
```

Delta is negative because the jackpot is money leaving the house. `game_type` is the literal string `"jackpot_payout"` (not the actual game type) so `get_house_report()` picks it up as a distinct line item in its GROUP BY.

- [ ] **Step 2: Commit**

```bash
git add casino/casino_db.py
git commit -m "fix(casino): record jackpot payouts in house bank ledger (GAP 6 fix 2)"
```

---

### Task 4: Add Backfill Function for Historical Jackpot Transactions

**Files:**
- Modify: `casino/casino_db.py` (add new function at module level, after `seed_jackpot` around line 529)

- [ ] **Step 1: Add `backfill_jackpot_tags()` function**

After the `seed_jackpot()` function (~line 529), add:

```python
async def backfill_jackpot_tags() -> int:
    """
    One-time migration: tag historical jackpot credit transactions with
    subsystem='CASINO' and subsystem_id='JP_<log_id>'.
    Idempotent — only affects rows where subsystem IS NULL.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE transactions SET subsystem='CASINO', subsystem_id='JP_' || (
                SELECT jl.id FROM casino_jackpot_log jl
                WHERE jl.discord_id = transactions.discord_id
                  AND jl.amount = transactions.amount
                  AND ABS(julianday(jl.won_at) - julianday(transactions.created_at)) < 0.001
                ORDER BY ABS(julianday(jl.won_at) - julianday(transactions.created_at))
                LIMIT 1
            )
            WHERE transactions.description LIKE 'JACKPOT%'
              AND transactions.subsystem IS NULL
        """)
        count = cursor.rowcount
        await db.commit()
    return count
```

The `0.001` day threshold (~86 seconds) is wide enough to tolerate any clock skew between the jackpot log INSERT and the wallet credit (they happen in the same function, so typically < 1ms apart), but narrow enough to avoid matching unrelated jackpots.

- [ ] **Step 2: Commit**

```bash
git add casino/casino_db.py
git commit -m "feat(casino): add backfill for historical jackpot transaction tags (GAP 6 fix 3)"
```

---

### Task 5: Hook Backfill at Startup + Version Bump

**Files:**
- Modify: `bot.py:166` (version string)
- Modify: `bot.py:236-238` (after existing wager_registry backfill)

- [ ] **Step 1: Bump version**

At `bot.py:166`, change:
```python
ATLAS_VERSION = "4.6.0"  # GAP 5: unified wager registry across all subsystems
```
to:
```python
ATLAS_VERSION = "4.7.0"  # GAP 6: jackpot payout tagging + house bank tracking
```

- [ ] **Step 2: Add backfill call after wager registry backfill**

At `bot.py:238` (after the wager_count print block), add:

```python
        from casino.casino_db import backfill_jackpot_tags
        jp_count = await backfill_jackpot_tags()
        if jp_count:
            print(f"ATLAS: Jackpot tags — backfilled {jp_count} transactions.")
```

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "feat(bot): hook jackpot backfill at startup, bump to v4.7.0 (GAP 6)"
```

---

### Task 6: Verification

- [ ] **Step 1: Start the bot and check startup output**

Run: `python bot.py`

Expected: Bot starts cleanly. If historical jackpot transactions exist with `subsystem IS NULL`, the backfill count is printed. If no untagged jackpots exist, no message is printed.

- [ ] **Step 2: Verify backfill idempotency**

Stop and restart the bot. Expected: No backfill message on second run (all rows already tagged, `UPDATE ... WHERE subsystem IS NULL` affects 0 rows).

- [ ] **Step 3: Verify house report picks up jackpot_payout**

Query directly:
```sql
SELECT game_type, SUM(delta) FROM casino_house_bank GROUP BY game_type;
```
Expected: If any jackpots have been triggered, a `jackpot_payout` row appears with negative delta.

- [ ] **Step 4: Final commit (squash if desired)**

All changes are in 4 focused commits across 2 files. No squash needed unless preferred.

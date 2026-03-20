# Parlay Leg Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize parlay legs from a JSON blob into a relational `parlay_legs` table with per-leg status tracking, enabling SQL-native analytics queries.

**Architecture:** Add a new `parlay_legs` table alongside the existing `parlays_table`. Dual-write on placement (JSON + relational). Refactor grading loops to iterate all legs and record per-leg outcomes. Backfill existing data at startup.

**Tech Stack:** Python 3.14, SQLite via sync sqlite3 (`_db_con()` pattern), discord.py 2.3+

**Spec:** `docs/superpowers/specs/2026-03-20-parlay-leg-normalization-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `flow_sportsbook.py` | Modify | Schema migration, dual-write placement, grading loop refactor, cancellation optimization, backfill function |
| `bot.py` | Modify | Call backfill at startup, bump version |

No new files created. No other files modified.

---

### Task 1: Schema Migration — Create `parlay_legs` Table

**Files:**
- Modify: `flow_sportsbook.py:124-167` (inside `setup_db()`)

- [ ] **Step 1: Add CREATE TABLE + indexes after `parlays_table` creation**

In `flow_sportsbook.py`, inside `setup_db()`, add the following after the `parlays_table` CREATE (after line 161):

```python
        con.execute("""
            CREATE TABLE IF NOT EXISTS parlay_legs (
                leg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                parlay_id  TEXT    NOT NULL REFERENCES parlays_table(parlay_id),
                leg_index  INTEGER NOT NULL,
                game_id    TEXT    NOT NULL,
                matchup    TEXT    NOT NULL,
                pick       TEXT    NOT NULL,
                bet_type   TEXT    NOT NULL,
                line       REAL    NOT NULL DEFAULT 0,
                odds       INTEGER NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'Pending',
                UNIQUE(parlay_id, leg_index)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay  ON parlay_legs(parlay_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_game    ON parlay_legs(game_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_matchup ON parlay_legs(matchup)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_parlay_legs_type    ON parlay_legs(bet_type, status)")
```

- [ ] **Step 2: Verify syntax compiles**

Run: `python -c "import flow_sportsbook"`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add flow_sportsbook.py
git commit -m "feat(sportsbook): add parlay_legs table schema migration"
```

---

### Task 2: Backfill Function — Populate `parlay_legs` from Existing JSON

**Files:**
- Modify: `flow_sportsbook.py` (add new function near end of file, before the Cog class)
- Modify: `bot.py:228-237` (startup hook)

- [ ] **Step 1: Add `_derive_historical_leg_status` helper and `backfill_parlay_legs_sync` function**

Add to `flow_sportsbook.py` after `setup_db()` and before the Cog class definition:

```python
def _derive_historical_leg_status(parlay_status: str) -> str:
    """Map parlay-level status to individual leg status for historical backfill."""
    if parlay_status == "Won":
        return "Won"
    if parlay_status in ("Cancelled",):
        return "Cancelled"
    if parlay_status == "Pending":
        return "Pending"
    # Lost, Push, Error — can't determine individual leg outcomes
    return "Unknown"


def backfill_parlay_legs_sync() -> int:
    """Populate parlay_legs from existing JSON legs column. Returns rows inserted."""
    count = 0
    with _db_con() as con:
        rows = con.execute(
            "SELECT parlay_id, legs, status FROM parlays_table WHERE legs IS NOT NULL"
        ).fetchall()

        batch = 0
        for parlay_id, legs_json, parlay_status in rows:
            existing = con.execute(
                "SELECT 1 FROM parlay_legs WHERE parlay_id=? LIMIT 1", (parlay_id,)
            ).fetchone()
            if existing:
                continue
            try:
                legs = json.loads(legs_json) if isinstance(legs_json, str) else []
            except Exception:
                log.warning("Corrupt parlay JSON in backfill: pid=%s", parlay_id)
                continue
            for i, leg in enumerate(legs):
                leg_status = _derive_historical_leg_status(parlay_status)
                con.execute(
                    "INSERT OR IGNORE INTO parlay_legs "
                    "(parlay_id, leg_index, game_id, matchup, pick, bet_type, line, odds, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (parlay_id, i, leg.get("game_id", ""),
                     leg.get("matchup", ""), leg.get("pick", ""),
                     leg.get("bet_type", ""), leg.get("line", 0),
                     leg.get("odds", 0), leg_status),
                )
                count += 1
            batch += 1
            if batch % 100 == 0:
                con.commit()
        con.commit()
    return count
```

- [ ] **Step 2: Wire backfill into `bot.py` startup**

In `bot.py`, after the `flow_wallet` setup block (after line 237), add:

```python
    # Parlay legs backfill (normalize JSON → relational)
    try:
        import flow_sportsbook
        backfilled_legs = await asyncio.to_thread(flow_sportsbook.backfill_parlay_legs_sync)
        if backfilled_legs:
            print(f"ATLAS: Sportsbook — backfilled {backfilled_legs} parlay legs.")
    except Exception as e:
        print(f"ATLAS: Parlay legs backfill failed: {e}")
```

- [ ] **Step 3: Bump version in `bot.py`**

Change line 166 from:
```python
ATLAS_VERSION = "4.1.0"  # Unified audit: subsystem tagging + $0 loss settlement txns
```
To:
```python
ATLAS_VERSION = "4.2.0"  # Parlay leg normalization: relational parlay_legs table
```

- [ ] **Step 4: Verify syntax compiles**

Run: `python -c "import flow_sportsbook; import bot"`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add flow_sportsbook.py bot.py
git commit -m "feat(sportsbook): add parlay legs backfill migration + startup hook"
```

---

### Task 3: Dual-Write — Insert Legs on Parlay Placement

**Files:**
- Modify: `flow_sportsbook.py:1249-1276` (inside `ParlayWagerModal.on_submit()`)

- [ ] **Step 1: Add parlay_legs INSERT after parlays_table INSERT**

In `ParlayWagerModal.on_submit()`, after the `INSERT INTO parlays_table` (line 1265-1271) and before the `flow_wallet.update_balance_sync` call (line 1272), add:

```python
                    # Dual-write: insert normalized legs
                    for i, leg in enumerate(self.legs):
                        con.execute(
                            "INSERT INTO parlay_legs "
                            "(parlay_id, leg_index, game_id, matchup, pick, bet_type, line, odds) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (parlay_id, i, leg["game_id"], leg["matchup"], leg["pick"],
                             leg["bet_type"], leg.get("line", 0), leg["odds"]),
                        )
```

This is inside the same `BEGIN IMMEDIATE` transaction, so it's atomic with the JSON write and balance debit.

- [ ] **Step 2: Verify syntax compiles**

Run: `python -c "import flow_sportsbook"`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add flow_sportsbook.py
git commit -m "feat(sportsbook): dual-write parlay legs on placement"
```

---

### Task 4: Grading Loop Refactor — Auto-Grade Path

**Files:**
- Modify: `flow_sportsbook.py:999-1086` (auto-grade parlay grading in `_grade_bets_impl()`)

- [ ] **Step 1: Refactor auto-grade parlay loop to grade all legs**

Replace lines 1009-1033 (the flag variables and `for leg in legs` loop with `break`) with a loop that iterates all legs, records per-leg results, and collects outcomes:

```python
                        leg_results = []
                        for leg_idx, leg in enumerate(legs):
                            gd = _fuzzy_match(leg["matchup"].lower().strip(), scores)
                            if not gd:
                                leg_results.append("Pending")
                                continue
                            res = _grade_single_bet(
                                leg["bet_type"], leg["pick"], float(leg.get("line", 0)),
                                gd["home"], gd["away"], gd["home_score"], gd["away_score"]
                            )
                            leg_results.append(res)
                            # Record per-leg status
                            try:
                                con.execute(
                                    "UPDATE parlay_legs SET status=? WHERE parlay_id=? AND leg_index=?",
                                    (res, pid, leg_idx),
                                )
                            except Exception:
                                log.debug("parlay_legs UPDATE skipped for pid=%s leg=%d", pid, leg_idx)
```

- [ ] **Step 2: Replace flag-based outcome logic with `leg_results`-based logic**

Replace lines 1035-1086 (the `if unresolved > 0 and not any_lost` ... `elif any_pushed` block) with:

```python
                        # A single loss kills the parlay even if other legs are unresolved
                        any_lost = "Lost" in leg_results
                        has_pending = "Pending" in leg_results
                        all_won = all(r == "Won" for r in leg_results)
                        any_pushed = "Push" in leg_results

                        if has_pending and not any_lost:
                            continue

                        if all_won:
                            payout = _payout_calc(amt, c_odds)
                            if payout < 0 or payout > MAX_PAYOUT:
                                log.error(f"[AUTO-GRADE] Insane parlay payout ${payout:,.2f} for parlay {pid} — CAPPING")
                                con.execute("UPDATE parlays_table SET status='Error' WHERE parlay_id=?", (pid,))
                                settled += 1
                                continue
                            _update_balance(uid, payout, con,
                                            subsystem="PARLAY", subsystem_id=str(pid),
                                            reference_key=f"PARLAY_SETTLE_{pid}")
                            total_paid += payout - amt
                            con.execute("UPDATE parlays_table SET status='Won' WHERE parlay_id=?", (pid,))
                            wins += 1
                            pending_events.append({
                                "discord_id": uid,
                                "guild_id": None,
                                "source": "TSL_BET",
                                "bet_type": "parlay",
                                "amount": payout - amt,
                                "balance_after": _get_balance(uid),
                                "description": f"Won parlay (parlay_id={pid})",
                                "bet_id": pid,
                            })
                        elif any_lost:
                            con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                            losses += 1
                            pending_events.append({
                                "discord_id": uid,
                                "guild_id": None,
                                "source": "TSL_BET",
                                "bet_type": "parlay",
                                "amount": -amt,
                                "balance_after": _get_balance(uid),
                                "description": f"Lost parlay (parlay_id={pid})",
                                "bet_id": pid,
                            })
                        elif any_pushed:
                            _update_balance(uid, amt, con,
                                            subsystem="PARLAY", subsystem_id=str(pid),
                                            reference_key=f"PARLAY_PUSH_{pid}")
                            con.execute("UPDATE parlays_table SET status='Push' WHERE parlay_id=?", (pid,))
                            pushes += 1
                            pending_events.append({
                                "discord_id": uid,
                                "guild_id": None,
                                "source": "TSL_BET",
                                "bet_type": "parlay",
                                "amount": 0,
                                "balance_after": _get_balance(uid),
                                "description": f"Push parlay (parlay_id={pid})",
                                "bet_id": pid,
                            })
```

Note: The outcome logic is identical to before — `all_won`, `any_lost`, `any_pushed` are now derived from `leg_results` instead of mid-loop flags. The only behavioral change is removing the `break` on loss so all legs get graded.

- [ ] **Step 3: Verify syntax compiles**

Run: `python -c "import flow_sportsbook"`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add flow_sportsbook.py
git commit -m "feat(sportsbook): refactor auto-grade parlay loop for per-leg status"
```

---

### Task 5: Grading Loop Refactor — Manual Grade Path

**Files:**
- Modify: `flow_sportsbook.py:2196-2259` (manual grade parlay grading)

- [ ] **Step 1: Refactor manual-grade parlay loop identically to auto-grade**

Replace lines 2205-2259 with the same pattern used in Task 4. The manual grade path is nearly identical to the auto-grade path but uses `_update_balance` with subsystem params and `bet_log` instead of `pending_events`.

Replace the flag variables and `for leg in legs` loop (lines 2205-2219):

```python
                leg_results = []
                for leg_idx, leg in enumerate(legs):
                    gd = _fuzzy_match(leg["matchup"].lower().strip(), scores)
                    if not gd:
                        leg_results.append("Pending")
                        continue
                    res = _grade_single_bet(
                        leg["bet_type"], leg["pick"], float(leg.get("line", 0)),
                        gd["home"], gd["away"], gd["home_score"], gd["away_score"]
                    )
                    leg_results.append(res)
                    try:
                        con.execute(
                            "UPDATE parlay_legs SET status=? WHERE parlay_id=? AND leg_index=?",
                            (res, pid, leg_idx),
                        )
                    except Exception:
                        log.debug("parlay_legs UPDATE skipped for pid=%s leg=%d", pid, leg_idx)
```

Replace the outcome block (lines 2221-2259):

```python
                any_lost = "Lost" in leg_results
                has_pending = "Pending" in leg_results
                all_won = all(r == "Won" for r in leg_results)
                any_pushed = "Push" in leg_results

                if has_pending and not any_lost:
                    continue
                if all_won:
                    payout = _payout_calc(amt, c_odds)
                    if payout < 0 or payout > MAX_PAYOUT:
                        log.error(f"[GRADE] Insane parlay payout ${payout:,.2f} for parlay {pid} — CAPPING")
                        con.execute("UPDATE parlays_table SET status='Error' WHERE parlay_id=?", (pid,))
                        settled += 1
                        continue
                    _update_balance(uid, payout, con,
                                    subsystem="PARLAY", subsystem_id=str(pid),
                                    reference_key=f"PARLAY_SETTLE_{pid}")
                    total_paid += payout - amt
                    con.execute("UPDATE parlays_table SET status='Won' WHERE parlay_id=?", (pid,))
                    wins += 1
                    bet_log.append({"uid": uid, "result": "Won", "pick": "parlay",
                                    "bet_type": "parlay", "matchup": f"parlay_id={pid}",
                                    "wager": amt, "profit": payout - amt, "bet_id": pid})
                elif any_lost:
                    flow_wallet.update_balance_sync(
                        uid, 0, source="TSL_BET",
                        description=f"Lost: parlay {pid}",
                        reference_key=f"PARLAY_SETTLE_{pid}",
                        con=con, subsystem="PARLAY", subsystem_id=str(pid),
                    )
                    con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                    losses += 1
                    bet_log.append({"uid": uid, "result": "Lost", "pick": "parlay",
                                    "bet_type": "parlay", "matchup": f"parlay_id={pid}",
                                    "wager": amt, "profit": 0, "bet_id": pid})
                elif any_pushed:
                    _update_balance(uid, amt, con,
                                    subsystem="PARLAY", subsystem_id=str(pid),
                                    reference_key=f"PARLAY_PUSH_{pid}")
                    con.execute("UPDATE parlays_table SET status='Push' WHERE parlay_id=?", (pid,))
                    pushes += 1
                    bet_log.append({"uid": uid, "result": "Push", "pick": "parlay",
                                    "bet_type": "parlay", "matchup": f"parlay_id={pid}",
                                    "wager": amt, "profit": 0, "bet_id": pid})
```

- [ ] **Step 2: Verify syntax compiles**

Run: `python -c "import flow_sportsbook"`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add flow_sportsbook.py
git commit -m "feat(sportsbook): refactor manual-grade parlay loop for per-leg status"
```

---

### Task 6: Cancellation Path Optimization

**Files:**
- Modify: `flow_sportsbook.py:2612-2668` (inside `_sb_cancelgame_impl()`)

- [ ] **Step 1: Replace JSON parsing with `parlay_legs` query**

Replace the parlay cancellation block (lines 2612-2630). Also initialize `refund_users` here since we need to collect user IDs inside the `with _db_con()` block for the ledger posting that happens outside it:

```python
            # Also refund parlays containing this matchup (query normalized table)
            parlay_refunds = 0
            parlay_refund_users = set()  # collect UIDs for ledger posting outside this block
            affected_parlay_ids = con.execute(
                "SELECT DISTINCT parlay_id FROM parlay_legs "
                "WHERE LOWER(matchup) LIKE ? AND status='Pending'",
                (f"%{key}%",),
            ).fetchall()
            for (pid,) in affected_parlay_ids:
                row = con.execute(
                    "SELECT discord_id, wager_amount FROM parlays_table "
                    "WHERE parlay_id=? AND status='Pending'",
                    (pid,),
                ).fetchone()
                if not row:
                    continue
                uid, amt = row
                _update_balance(uid, amt, con)
                con.execute(
                    "UPDATE parlays_table SET status='Cancelled' WHERE parlay_id=?", (pid,)
                )
                con.execute(
                    "UPDATE parlay_legs SET status='Cancelled' "
                    "WHERE parlay_id=? AND status='Pending'",
                    (pid,),
                )
                parlay_refunds += 1
                total_refunded += amt
                parlay_refund_users.add(uid)
```

- [ ] **Step 2: Update the ledger posting block (lines 2644-2668)**

The ledger block at lines 2650-2657 still references `parlay_rows` (the old full-table scan variable). Replace the parlay portion (lines 2650-2657) to use the `parlay_refund_users` set collected in Step 1:

```python
            for uid in parlay_refund_users:
                refund_users.add(uid)
```

The `refund_users` set is already initialized at line 2647 for straight bet refund UIDs. We just add the parlay refund UIDs to it.

- [ ] **Step 3: Verify syntax compiles**

Run: `python -c "import flow_sportsbook"`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add flow_sportsbook.py
git commit -m "feat(sportsbook): use parlay_legs table for cancellation lookups"
```

---

### Task 7: Final Verification

- [ ] **Step 1: Full syntax check**

Run: `python -c "import flow_sportsbook; import bot"`
Expected: No errors

- [ ] **Step 2: Verify schema creates cleanly**

Run: `python -c "from flow_sportsbook import setup_db; setup_db(); print('OK')"`
Expected: `OK` with no errors

- [ ] **Step 3: Verify backfill runs**

Run: `python -c "from flow_sportsbook import setup_db, backfill_parlay_legs_sync; setup_db(); n = backfill_parlay_legs_sync(); print(f'Backfilled {n} legs')"`
Expected: Prints number of legs backfilled (should be > 0 if parlays exist)

- [ ] **Step 4: Verify idempotency**

Run the same backfill command again.
Expected: `Backfilled 0 legs` (already populated)

- [ ] **Step 5: Spot-check data integrity**

Run: `python -c "from flow_sportsbook import _db_con; c = _db_con().__enter__(); print('parlay_legs rows:', c.execute('SELECT COUNT(*) FROM parlay_legs').fetchone()[0]); print('parlays_table rows:', c.execute('SELECT COUNT(*) FROM parlays_table').fetchone()[0])"`

Expected: `parlay_legs` row count should be roughly 2-4x the `parlays_table` count (each parlay has multiple legs).

- [ ] **Step 6: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "feat(sportsbook): parlay leg normalization v4.2.0"
```

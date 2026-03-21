# ATLAS 3.0 Verification Report

**Version**: 3.0.0 | **Date**: 2026-03-18 | **Reviewer**: Independent code review (Claude)

---

## 3.0 Fixes — Verification Status

| Fix | Status | Evidence |
|-----|--------|----------|
| Per-user locks (`flow_wallet.get_user_lock`) | **SOLID** | Used at 7 call sites: `flow_sportsbook.py:1182,1256,1337`, `casino_db.py:923`, `economy_cog.py:98,127,158`, `polymarket_cog.py:766` |
| SSRF whitelist (`_validate_image_url`) | **SOLID** | Defined at `sentinel_cog.py:62`, called at lines 756, 1203, 1206, 1209, 2387 |
| Permission guards (`boss_cog.py`) | **MOSTLY SOLID** | 90+ `is_commissioner()` checks on buttons; gap in modal `on_submit()` — see MEDIUM-1 |
| MAX_PAYOUT cap (`flow_sportsbook.py`) | **BUG** | Cap checked in 4 places, but `continue` skips status update — see HIGH-1 |
| `BEGIN IMMEDIATE` transactions | **SOLID** | Correctly used in wallet, casino, polymarket |
| Migration guard (`setup_cog.py`) | **SOLID** | Idempotent `_migration_v2` flag at line 304, non-fatal on error |

---

## New Issues (Not in Prior Review)

**CRITICAL: 0 | HIGH: 3 | MEDIUM: 4**

---

### HIGH-1: MAX_PAYOUT skip leaves bet stuck as "Pending" forever

**File**: `flow_sportsbook.py`
**Lines**: 968–982 (autograde), 2176–2189 (manual grade) — 4 identical instances

**Bug**: When `payout > MAX_PAYOUT`, the code does `continue` which skips **both** the `_update_balance()` call **and** the `UPDATE bets_table SET status=?` statement that comes after it (line 981/2189). The bet remains "Pending" permanently. Every subsequent autograde cycle re-evaluates it, logs the same error, and skips again — infinite error log loop with zero user feedback.

```python
# Current (BROKEN) — line 968-982
if res == "Won":
    payout = _payout_calc(amt, int(odds))
    if payout < 0 or payout > MAX_PAYOUT:
        log.error(f"[AUTO-GRADE] Insane payout ${payout:,.2f} for bet {bid} — SKIPPING")
        continue                    # ← skips status update on line 981 too!
    _update_balance(uid, payout, con)
    ...
elif res == "Lost":
    losses += 1
con.execute("UPDATE bets_table SET status=? WHERE bet_id=?", (res, bid))  # never reached
```

**Fix**: Update status before `continue` so the bet is not re-processed:

```python
if payout < 0 or payout > MAX_PAYOUT:
    log.error(f"[AUTO-GRADE] Insane payout ${payout:,.2f} for bet {bid} — CAPPING")
    con.execute("UPDATE bets_table SET status='Error' WHERE bet_id=?", (bid,))
    settled += 1
    continue
```

Apply to all 4 locations: autograde straight (line 970), autograde parlay (line 1043), manual grade straight (line 2178), manual grade parlay (line 2230).

---

### HIGH-2: Polymarket WagerModal doesn't verify market is still active

**File**: `polymarket_cog.py`
**Lines**: 752–807 (`WagerModal.on_submit`)

**Bug**: The modal captures `market_id` when it opens but never checks `prediction_markets.status == 'active'` inside the transaction before inserting the contract. Race timeline:

1. User clicks "Buy YES" → modal opens (market is active)
2. Background `_auto_resolve_pass` resolves the market → `status='closed'`
3. User submits modal → debit succeeds, contract inserted with `status='open'`
4. Contract is **orphaned** — market already resolved, user's bucks gone forever

**Fix**: Inside the `BEGIN IMMEDIATE` block (after line 771), add a market status check:

```python
async with db.execute(
    "SELECT status FROM prediction_markets WHERE market_id = ?",
    (self.market_id,)
) as cur:
    mkt_row = await cur.fetchone()
if not mkt_row or mkt_row[0] != 'active':
    return await interaction.response.send_message(
        "This market is no longer active.", ephemeral=True)
```

---

### HIGH-3: No MAX_PAYOUT cap in casino or polymarket payouts

**File**: `casino/casino_db.py` (line 832–839), `polymarket_cog.py` (`_resolve`, line 3330)

**Bug**: `MAX_PAYOUT` (10M) is only enforced in `flow_sportsbook.py`. Casino payouts (including jackpot hits) and prediction market payouts have no upper bound. A slots jackpot accumulation or high-quantity prediction bet could produce an arbitrarily large payout with no safety net.

**Fix**: Define and enforce a payout cap in each vertical before calling `flow_wallet.credit()`:

- `casino/casino_db.py` — cap `payout` before line 834
- `polymarket_cog.py` — cap `payout` before line 3330 in `_resolve()`

---

### MEDIUM-1: Boss modal `on_submit()` methods lack permission re-verification

**File**: `boss_cog.py`
**Lines**: 880, 1124, 1175, 1214, 1357, 1368, 1386, and ~15 others

**Bug**: All 20+ modal `on_submit()` methods delegate to `_impl` methods without re-checking `is_commissioner()`. Discord modals have unique nonce-based custom_ids making direct exploitation unlikely, but defense-in-depth says admin-mutating modals should re-verify.

**Highest-risk modals** (modify balances/settings):
- `BossTreasuryGiveModal.on_submit` (line 1124) — gives/takes/sets user balances
- `BossCasinoLimitsModal.on_submit` (line 880) — changes casino bet limits
- `BossResolveModal.on_submit` (line 1357) — resolves prediction markets

**Fix**: Add `if not await is_commissioner(interaction): return` as first line in `on_submit` for treasury, casino, and market resolution modals at minimum.

---

### MEDIUM-2: `build_member_db.build_member_table()` blocks the event loop

**File**: `build_member_db.py`, ~lines 1076–1160

**Bug**: Uses synchronous `sqlite3.connect()` + `BEGIN EXCLUSIVE` in the main async context. `BEGIN EXCLUSIVE` locks the entire `tsl_history.db` file for 1–5 seconds while iterating 88+ member upserts. During this window, any async code awaiting SQLite access to `tsl_history.db` will stall.

**Risk**: Only triggers at startup and manual `/reload`. Low frequency, but during the lock window all history queries block.

**Fix**: Wrap in `loop.run_in_executor()` to move the sync work off the event loop.

---

### MEDIUM-3: Polymarket auto-resolve TOCTOU between status check and resolution

**File**: `polymarket_cog.py`, lines 2313–2372

**Bug**: `_auto_resolve_pass` checks `resolved_by` at line 2343 in one DB connection, then calls `_resolve()` at line 2372 which opens a **new** connection. An admin resolving the same market via `/boss` between these two calls could trigger `_resolve()` twice.

**Mitigating factor**: `_resolve()` uses `BEGIN IMMEDIATE` and only processes contracts with `status='open'`, so the second resolution finds zero contracts and produces zero payouts. **Not a data loss bug**, but generates redundant DB writes and confusing log entries.

**Fix**: Move the `resolved_by` guard check inside `_resolve()` itself, within its `BEGIN IMMEDIATE` transaction.

---

### MEDIUM-4: `_auto_resolve_pass` redundantly double-updates market status

**File**: `polymarket_cog.py`
**Lines**: 3350–3355 (`_resolve` sets `status='closed', resolved_by=?`) + 2375–2382 (`_auto_resolve_pass` overwrites `resolved_by='auto'` again)

**Bug**: `_resolve()` already marks the market as closed with the correct `resolved_by` value. Then `_auto_resolve_pass` opens a separate connection and overwrites `resolved_by` to `'auto'` redundantly. Two transactions touching the same row for no reason.

**Fix**: Remove the second UPDATE in `_auto_resolve_pass` (lines 2375–2382), since `_resolve()` already handles it.

---

## Integration Risk Assessment

| Interaction | Risk | Analysis |
|-------------|------|----------|
| Per-user lock + polymarket WagerModal | **None** | Lock acquired at line 766 before transaction. No nested locks. |
| Per-user lock + casino process_wager | **None** | Lock at `casino_db.py:923`, single `BEGIN IMMEDIATE` inside. Clean. |
| Cooldown decorators + error handlers | **None** | Standard discord.py cooldowns; no conflict with `on_error`. |
| Migration guard + first-run | **Safe** | `_migration_v2` idempotent; table creation precedes migration. |
| MAX_PAYOUT + autograde loop | **Bug** | See HIGH-1 — the cap itself is fine but the `continue` breaks status tracking. |

---

## Areas That Passed Review

- **SQL injection**: All queries parameterized. `oracle_query_builder.py` has whitelist + keyword blocking. `codex_cog.py` parameterizes Gemini-generated SQL.
- **Playwright resources**: `atlas_html_engine.py` PagePool uses try/finally, page recycling, `drain()` on shutdown.
- **Discord API compliance**: Select menus sliced to 25. `defer()` before Gemini calls. `view=None` only in `edit_message()`.
- **Per-user lock design**: Fine-grained, no deadlock risk, no nested acquisition.
- **Wallet transactions**: `BEGIN IMMEDIATE` + rollback + `reference_key` idempotency is solid.

# GAP 6: Jackpot Payouts Audit — Design Spec

## Context

Casino jackpot payouts (`_award_jackpot()` in `casino/casino_db.py:480-518`) call `flow_wallet.credit()` **without** `subsystem` or `subsystem_id` parameters. This means:
- Jackpot credit transactions have `subsystem=NULL` in the transactions table
- They're invisible to subsystem-filtered queries and the unified wager registry (GAP 5)
- The 1% wager contribution to jackpot pools is not reflected in the house bank ledger

## Fix 1: Tag Jackpot Credit Calls

### Current code (`casino_db.py:496-501`)
```python
await flow_wallet.credit(
    discord_id, amount, "CASINO",
    description=f"JACKPOT {tier.upper()} win!",
    reference_key=ref_key,
    con=con,
)
```

### Fixed code
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

### Required plumbing

`_award_jackpot()` doesn't currently receive `session_id`. The call chain is:

```
process_wager() → line 851: CREATE casino_sessions → session_id available
                → line 931: _contribute_and_check_jackpot(discord_id, wager, game_type, streak_len, con)
                             → _award_jackpot(tier, discord_id, game_type, con)
```

Both `_contribute_and_check_jackpot()` and `_award_jackpot()` need `session_id` added as a parameter.

**New signatures:**
- `_contribute_and_check_jackpot(discord_id, wager, game_type, streak_len, con, session_id)`
- `_award_jackpot(tier, discord_id, game_type, con, session_id)`

## Fix 2: House Bank Jackpot Payout Entry

### Why NOT track jackpot contributions in house_bank

The 1% wager contribution to jackpot pools is already implicitly captured in the per-game `house_delta` (`wager - payout`). The contribution comes from the wager amount the house already received. Adding a negative `jackpot_feed` entry would double-count the loss. The jackpot pool is an internal house allocation, not an outflow to a player.

### Payout tracking (the real outflow)

When a jackpot is awarded, money genuinely leaves the house to a player. Add a `jackpot_payout` entry:

```python
await con.execute(
    "INSERT INTO casino_house_bank (game_type, delta, session_id, recorded_at) VALUES (?,?,?,?)",
    ("jackpot_payout", -amount, session_id, now),
)
```

Delta is negative because jackpot payouts are money leaving the house to the player. This is distinct from the game's `house_delta` which only accounts for the game outcome payout (not jackpot bonus).

### Net effect on house bank reporting

- Per-game `house_delta` remains unchanged
- New `jackpot_payout` entries show large jackpot payouts as distinct house losses
- `get_house_report()` will automatically pick up `jackpot_payout` in its GROUP BY

## Fix 3: Backfill Historical Jackpot Transactions

### Data source

`casino_jackpot_log` has all historical jackpot wins:
```sql
casino_jackpot_log (id, tier, discord_id, amount, game_type, won_at)
```

### Backfill strategy

1. **Tag existing transactions:** Match jackpot credit transactions to `casino_jackpot_log` entries using a correlated subquery with amount + timestamp proximity to avoid ambiguous matches when the same player wins the same tier twice:
   ```sql
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
   ```

   The `< 0.001` day threshold (~86 seconds) is wide enough to tolerate clock skew between the jackpot log INSERT and the wallet credit, but narrow enough to avoid matching unrelated jackpots. The `LIMIT 1 ORDER BY` picks the closest match when multiple candidates exist.

   Since jackpot payouts don't link to a session_id historically, use `'JP_' + log_id` as a synthetic subsystem_id to distinguish them.

2. **No wager registry entries for jackpots:** Jackpots are not wagers — they're bonus payouts on existing wagers. The underlying casino wager is already registered. Adding jackpots to the wagers table would be incorrect (no wager_amount, no odds).

### Idempotency

The UPDATE only affects rows where `subsystem IS NULL`, so re-running is safe. Use a dedicated backfill function called at startup.

## Files Changed

| File | Change |
|------|--------|
| `casino/casino_db.py` | Add `session_id` param to `_contribute_and_check_jackpot()` and `_award_jackpot()`; add subsystem tags to credit call; add house_bank entries; add backfill function |
| `bot.py` | Hook backfill at startup; version bump 4.6.0 → 4.7.0 |

## Verification

1. **Check transactions for existing jackpot wins** → verify `subsystem='CASINO'` and `subsystem_id` is populated (backfill)
2. **Trigger a jackpot** (admin seed + boost) → verify credit transaction has `subsystem='CASINO', subsystem_id=str(session_id)`; verify `jackpot_payout` entry in house_bank
3. **House report** → `get_house_report()` includes `jackpot_payout` line items
4. **Backfill idempotency** → run twice, no duplicate updates

# GAP 7: House Bank as Derived View — Design Spec

**Date:** 2026-03-20
**Status:** Approved
**Depends on:** GAP 5 (wager registry), GAP 6 (jackpot audit)

---

## Problem

The `casino_house_bank` table is a write-through ledger tracking house P&L per game. Since GAP 5 introduced a unified wager registry (`wagers` table), house P&L can be derived as `-SUM(result_amount)` — but two write paths are missing from the registry:

1. **PvP coinflip** — no `register_wager()`, no `settle_wager()`, credit missing subsystem tags
2. **Jackpot payouts** — not represented in wager_registry at all

## Solution

1. Integrate PvP coinflip with wager_registry (new subsystem `CASINO_PVP`)
2. Add synthetic zero-wager entries for jackpot payouts (new subsystem `CASINO_JACKPOT`)
3. Backfill historical PvP + jackpot data into wager_registry
4. Rewrite `get_house_report()` to derive P&L from `wagers` table
5. Remove `casino_house_bank` writes (keep table as archive for 1 release)

## Key Formula

```
house_profit = -SUM(result_amount)
```

This works for all game types:
- Regular games: `result_amount = payout - wager`, so house gets `wager - payout`
- PvP (2 entries): winner `+0.9W` + loser `-W` = `-0.1W`, house gets `+0.1W`
- Jackpot: `result_amount = payout`, house gets `-payout`

## Subsystem ID Schemes

| Subsystem | ID Pattern | Example |
|-----------|-----------|---------|
| `CASINO` | `{session_id}` (backlinked from correlation_id) | `142` |
| `CASINO_PVP` | `PVP_{challenge_id}_{C\|O}` | `PVP_17_C` |
| `CASINO_JACKPOT` | `JP_{session_id}_{tier}` | `JP_142_mini` |
| `CASINO_PVP` (backfill) | `PVP_LEGACY_{session_id}` | `PVP_LEGACY_88` |
| `CASINO_JACKPOT` (backfill) | `JP_LEGACY_{log_id}` | `JP_LEGACY_3` |

## Schema Changes

### coinflip_challenges — new columns
```sql
ALTER TABLE coinflip_challenges ADD COLUMN challenger_corr_id TEXT DEFAULT NULL;
ALTER TABLE coinflip_challenges ADD COLUMN opponent_corr_id TEXT DEFAULT NULL;
```

### casino_house_bank — no schema change (kept as archive, writes removed)

## Files Modified

| File | Changes |
|------|---------|
| `casino/casino_db.py` | Schema migration, `create_challenge()`, `resolve_challenge()`, `_award_jackpot()`, `get_house_report()`, remove house_bank writes |
| `casino/games/coinflip.py` | Pass correlation_ids through challenge flow |
| `wager_registry.py` | `backfill_pvp_wagers()`, `backfill_jackpot_wagers()` |
| `bot.py` | Hook backfills at startup, version bump to 4.8.0 |

## Verification

- Compare `SUM(delta) FROM casino_house_bank` vs `-SUM(result_amount) FROM wagers` after backfill
- Run house report command and verify P&L numbers match pre-migration values
- Play test games of each type to verify forward-looking wager registration

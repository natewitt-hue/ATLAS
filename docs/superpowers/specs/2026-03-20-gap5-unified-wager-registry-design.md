# GAP 5: Unified Wager Registry — Design Spec

## Context

ATLAS's Flow Economy has 5 subsystems that create wagers (sportsbook straight bets, parlays, props, casino, predictions). Each stores wager data in its own table with different schemas, ID types, and lifecycle states. The `transactions` table links to source tables via `subsystem` + `subsystem_id`, but there is no way to query "all wagers for user X" without knowing which table to JOIN per subsystem.

GAP 5 adds a **wager registry table** — a single write-through table that every subsystem populates at wager placement and updates at settlement. This enables cross-subsystem wager queries, aggregate P&L reporting, and unified history views.

## Schema

```sql
CREATE TABLE IF NOT EXISTS wagers (
    wager_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subsystem      TEXT    NOT NULL,
    subsystem_id   TEXT    NOT NULL,
    discord_id     INTEGER NOT NULL,
    wager_amount   INTEGER NOT NULL,
    odds           INTEGER,
    label          TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'open',
    result_amount  INTEGER,
    created_at     TEXT    NOT NULL,
    settled_at     TEXT,
    UNIQUE(subsystem, subsystem_id)
);

CREATE INDEX IF NOT EXISTS idx_wagers_user   ON wagers(discord_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wagers_status ON wagers(status, subsystem);
CREATE INDEX IF NOT EXISTS idx_wagers_sub    ON wagers(subsystem, subsystem_id);
```

### Column semantics

| Column | Type | Notes |
|--------|------|-------|
| `subsystem` | TEXT NOT NULL | `TSL_BET`, `PARLAY`, `PROP`, `CASINO`, `PREDICTION` |
| `subsystem_id` | TEXT NOT NULL | Source table PK cast to TEXT. Matches `transactions.subsystem_id`. |
| `discord_id` | INTEGER NOT NULL | Normalized — prediction's TEXT `user_id` is cast to int. |
| `wager_amount` | INTEGER NOT NULL | Amount risked (always positive). |
| `odds` | INTEGER NULL | American odds. NULL for casino (uses multiplier) and predictions (uses buy_price). |
| `label` | TEXT | Human-readable display string. E.g. `"KC ML +150"`, `"Blackjack"`, `"MVP: Mahomes YES @ $0.65"`. |
| `status` | TEXT | Lowercase: `open`, `won`, `lost`, `push`, `voided`. Source tables use mixed case — registry normalizes. |
| `result_amount` | INTEGER NULL | Net P&L. Win=$profit, Loss=-wager, Push=0. NULL while open. `SUM(result_amount)` = total P&L. |
| `settled_at` | TEXT NULL | ISO timestamp. NULL while open. |

### Composite uniqueness

`UNIQUE(subsystem, subsystem_id)` prevents double-registration. This is the natural key since subsystem_id values are only unique within a subsystem (bet_id=1 and session_id=1 can coexist).

## Module: `wager_registry.py`

New file at repo root. Follows flow_wallet.py conventions: sync + async duality, `con=None` for transaction joining.

### API

```
ensure_wager_table(db)              — async — DDL, called from setup_wallet_db()
register_wager(subsystem, subsystem_id, discord_id, wager_amount, label, odds, *, con)  — async
register_wager_sync(...)            — sync  — for flow_sportsbook.py code paths
settle_wager(subsystem, subsystem_id, status, result_amount, *, con)                    — async
settle_wager_sync(...)              — sync
get_active_wagers(discord_id)       — async — all open wagers across subsystems
get_wager_history(discord_id, limit)— async — recent settled wagers
get_wager_summary(discord_id)       — async — aggregate P&L by subsystem
backfill_wagers()                   — async — one-time migration
```

All mutating functions accept `con=None`. When `con` is provided, the function joins the caller's transaction (no auto-commit). When None, opens its own connection.

## Integration Points

### Wager Placement (6 sites)

| # | Subsystem | File | Line | Sync/Async | Label example |
|---|-----------|------|------|------------|---------------|
| 1 | TSL_BET | `flow_sportsbook.py` | ~1219 | sync | `"KC ML +150"` |
| 2 | PARLAY | `flow_sportsbook.py` | ~1304 | sync | `"Parlay (3L): KC/BUF/DEN"` |
| 3 | PROP | `flow_sportsbook.py` | ~1388 | sync | `"Prop #12: Option A"` |
| 4 | CASINO | `casino/casino_db.py` | ~966 | async | `"Casino Blackjack"` |
| 5 | PREDICTION | `polymarket_cog.py` | ~791 | async | `"mvp-race: YES @ $0.65"` |

Each call is placed **after** the source table INSERT and wallet debit, **inside the same transaction** (using the `con` parameter).

**Casino exception:** `deduct_wager()` does not accept a `con` parameter — it calls `flow_wallet.debit()` with its own internal connection. The registry write is a separate call after the debit returns. This is a small atomicity gap: if `register_wager()` fails after a successful debit, the user is debited but has no registry entry. This is acceptable because (a) the backfill can repair inconsistencies, and (b) the wager will still appear in `transactions` via subsystem tagging. The registry is supplementary, not authoritative for balance.

### Wager Settlement (5 subsystems, ~10 code sites)

| # | Subsystem | File | Lines | Sync/Async | Notes |
|---|-----------|------|-------|------------|-------|
| 1 | TSL_BET | `flow_sportsbook.py` | ~2288–2305 | sync | Manual grading |
| 2 | PARLAY (auto) | `flow_sportsbook.py` | ~1064–1096 | sync | Auto-grading in `_grade_async()` |
| 3 | PARLAY (manual) | `flow_sportsbook.py` | ~2360–2382 | sync | Manual grading |
| 4 | PROP | `flow_sportsbook.py` | ~2939–2960 | sync | Manual grading |
| 5 | CASINO | `casino/casino_db.py` | ~860–897 | async | `complete_game()` |
| 6 | CASINO refund | `casino/casino_db.py` | ~971 | async | `refund_wager()` → voided |
| 7 | PREDICTION | `polymarket_cog.py` | ~3286–3307 | async | Contract resolution loop |
| 8 | TSL_BET cancel | `flow_sportsbook.py` | ~2720–2739 | sync | `_sb_cancelgame_impl()` — cancel all pending bets for a matchup → voided |
| 9 | PARLAY cancel | `flow_sportsbook.py` | ~2742–2770 | sync | `_sb_cancelgame_impl()` — cancel parlays containing cancelled matchup → voided |
| 10 | TSL_BET refund | `flow_sportsbook.py` | ~2803–2823 | sync | `_sb_refund_impl()` — refund single bet by ID → voided |

### `result_amount` Computation Rules

| Outcome | Formula | Example ($100 bet at +150) |
|---------|---------|---------------------------|
| **Won** | `payout - wager_amount` (profit only) | `250 - 100 = 150` |
| **Lost** | `-wager_amount` | `-100` |
| **Push** | `0` | `0` |
| **Voided** | `0` (refund restores original wager) | `0` |

At each settlement site, `wager_amount` (aliased as `amt`) is in scope from the SELECT that fetches the bet/parlay/prop row. For wins, `payout` is calculated from the American odds formula: `amt * (odds/100)` for positive odds, `amt * (100/abs(odds))` for negative odds, plus the original `amt`.

## Casino Dual-ID Strategy

Casino wagers have a two-phase lifecycle:

1. **Debit** (`deduct_wager`, line ~966): Only `correlation_id` is known. Register wager with `subsystem_id=correlation_id`.
2. **Settlement** (`complete_game`, line ~860): `session_id` is created. UPDATE the wager's `subsystem_id` from correlation_id to session_id, then call `settle_wager()`.
3. **Refund** (`refund_wager`, line ~971): No session_id exists. Settle as `voided` using `correlation_id`.

This mirrors the existing backlink UPDATE on the `transactions` table at `casino_db.py:897`.

## Backfill Migration

`backfill_wagers()` populates the registry from existing source tables:

| Source | Target subsystem | Notes |
|--------|-----------------|-------|
| `bets_table` (WHERE parlay_id IS NULL) | TSL_BET | Exclude parlay leg rows |
| `parlays_table` | PARLAY | — |
| `prop_wagers` | PROP | — |
| `casino_sessions` | CASINO | All rows are already settled |
| `prediction_contracts` | PREDICTION | Cast `user_id` to int |

- Uses `INSERT OR IGNORE` + UNIQUE constraint for idempotency.
- Early-exits if table already has rows (optimization only — INSERT OR IGNORE is the real guard).
- Settled timestamps: use `casino_sessions.played_at` and `prediction_contracts.resolved_at` where available. For sportsbook bets/parlays/props (which lack a `settled_at` column), use `created_at` as approximation.
- Payout calculation for result_amount uses the standard American odds formula inline (avoids circular import from flow_sportsbook).
- Called once at bot startup from `bot.py`, after `setup_wallet_db()`.

## Files Changed

| File | Scope |
|------|-------|
| **`wager_registry.py`** (NEW) | All registry logic: DDL, register/settle helpers, query helpers, backfill |
| `flow_wallet.py` | 1 line added in `setup_wallet_db()`: call `ensure_wager_table(db)` |
| `bot.py` | Import + backfill call at startup; version bump 4.5.0 → 4.6.0 |
| `flow_sportsbook.py` | ~18 lines added across 9 integration sites (3 placement, 6 settlement/cancel) |
| `casino/casino_db.py` | ~8 lines added across 3 integration sites |
| `polymarket_cog.py` | ~6 lines added across 2 integration sites |

## Out of Scope

- **Display consumer migration:** Updating flow_cards.py, sportsbook_cards.py, or history commands to read from the wagers table. The registry augments source tables; consumers adopt it incrementally in future work.
- **Admin wagers:** ADMIN subsystem has no source wager (give/take/set are balance ops). Not registered.
- **Stale wager cleanup:** Casino wagers that never settle (crashes, disconnects) remain `open`. A periodic cleanup job is a future enhancement.

## Verification

1. **Startup:** Bot starts cleanly, `wagers` table is created, backfill runs and logs count.
2. **Straight bet:** Place a bet via `/bet` → verify row in `wagers` with status=open. Grade it → verify status updates to won/lost/push with correct result_amount.
3. **Parlay:** Place a parlay → verify row. Auto-grade → verify settlement.
4. **Prop:** Place a prop wager → verify row. Grade → verify settlement.
5. **Casino:** Play a blackjack hand → verify wager registered with correlation_id, then settled with session_id after game completes.
6. **Prediction:** Buy a contract → verify row. Resolve market → verify settlement.
7. **Cross-subsystem query:** `SELECT * FROM wagers WHERE discord_id=? ORDER BY created_at DESC` returns wagers from all subsystems in one query.
8. **Idempotency:** Run backfill twice → no duplicates (INSERT OR IGNORE).
9. **P&L aggregate:** `SELECT SUM(result_amount) FROM wagers WHERE discord_id=?` returns correct total P&L.

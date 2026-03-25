# ATLAS Unified Event-Bet Architecture

**Date:** 2026-03-24
**Status:** Approved — rev 2 (post spec review)
**Approach:** Shared Core Module — event-bus driven settlement + 10-min fallback poll

---

## Problem

Three independent bet-grading pipelines in ATLAS each own their own DB connection logic, matchup-to-score resolution, and grading loop:

| Pipeline | File | Matching method | Known failures |
|----------|------|-----------------|----------------|
| TSL/Madden | `flow_sportsbook.py` | Fuzzy string match on team names | Team name format mismatches → orphaned Pending bets |
| Real sports | `real_sportsbook_cog.py` | ESPN event ID (already FK) | Multiple writers → SQLite lock contention |
| Prediction markets | `polymarket_cog.py` | market_id polling | Duplicate settlement when loop fires twice |

## Solution

A single `sportsbook_core.py` module owns all grading logic. Every schedulable outcome is pre-written to a canonical `events` table with a stable `event_id` before any bet is placed. Every bet references `event_id` by foreign key — no matchup strings ever stored. A single settlement worker, triggered by `flow_bus.emit("event_finalized", ...)` (extending the existing `flow_events.py` bus), grades all linked bets with idempotent two-step commits.

---

## Database: `flow.db`

Replaces `sportsbook.db`. `flow_economy.db` (wallet/transactions) is **untouched**.

> **Note on DB split:** `bets` live in `flow.db`; wallet balances live in `flow_economy.db`. SQLite has no cross-file transactions. Settlement uses a deliberate two-step sequence (credit wallet first, then mark bet) protected by `reference_key` idempotency. See Settlement Flow section.

### Schema

```sql
PRAGMA foreign_keys = ON;  -- must be set on every connection to flow.db

-- ─── EVENTS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    -- 'tsl:{scheduleId}' | 'real:{sport_key}:{espn_id}' | 'poly:{market_id}'
    source            TEXT NOT NULL CHECK(source IN ('TSL','REAL','POLY')),
    status            TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK(status IN ('scheduled','live','final','cancelled')),
    home_participant  TEXT,
    away_participant  TEXT,
    home_score        REAL,   -- NULL until final
    away_score        REAL,
    result_payload    TEXT,   -- JSON: source-specific raw data
    commence_ts       TEXT,   -- ISO8601 UTC, e.g. '2026-03-24T20:30:00Z'
    finalized_ts      TEXT,   -- UTC timestamp when status → 'final'
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_events_source_status ON events(source, status);

-- ─── BETS ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bets (
    bet_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id    INTEGER NOT NULL,
    event_id      TEXT NOT NULL REFERENCES events(event_id),
    bet_type      TEXT NOT NULL CHECK(bet_type IN
                      ('Moneyline','Spread','Over','Under','Prediction')),
    pick          TEXT NOT NULL,
    line          REAL,           -- spread or O/U (NULL for ML/Prediction)
    odds          INTEGER NOT NULL,
    wager_amount  INTEGER NOT NULL,
    status        TEXT DEFAULT 'Pending'
                  CHECK(status IN ('Pending','Won','Lost','Push','Cancelled','Error')),
    parlay_id     TEXT REFERENCES parlays(parlay_id),
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_bets_event_status ON bets(event_id, status);
CREATE INDEX IF NOT EXISTS idx_bets_user         ON bets(discord_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bets_parlay       ON bets(parlay_id)
                                                    WHERE parlay_id IS NOT NULL;

-- ─── PARLAYS ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS parlays (
    parlay_id     TEXT PRIMARY KEY,
    discord_id    INTEGER NOT NULL,
    combined_odds INTEGER NOT NULL,
    wager_amount  INTEGER NOT NULL,
    status        TEXT DEFAULT 'Pending'
                  CHECK(status IN ('Pending','Won','Lost','Push','Cancelled')),
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_parlays_user_status ON parlays(discord_id, status);

-- ─── PARLAY LEGS ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS parlay_legs (
    leg_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id TEXT NOT NULL REFERENCES parlays(parlay_id),
    bet_id    INTEGER NOT NULL REFERENCES bets(bet_id),
    leg_index INTEGER NOT NULL,
    UNIQUE(parlay_id, leg_index)
);

CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay ON parlay_legs(parlay_id);

-- ─── EVENT LOCKS (bet-placement guard only, not settlement) ───────────────
-- Used by ingestor cogs to prevent new bets after game starts.
-- Settlement concurrency is handled by per-event asyncio locks in sportsbook_core.
CREATE TABLE IF NOT EXISTS event_locks (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    locked   INTEGER DEFAULT 0
);

-- ─── LINE OVERRIDES ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_line_overrides (
    event_id    TEXT PRIMARY KEY REFERENCES events(event_id),
    home_spread REAL, away_spread REAL,
    home_ml     INTEGER, away_ml INTEGER,
    ou_line     REAL,
    set_by TEXT, set_at TEXT
);

-- ─── PARLAY CART (unchanged — already has source + event_id columns) ──────
-- Retained with no schema changes. UNIQUE(discord_id, source, event_id)
-- constraint preserved — one leg per event per user per cart.

-- ─── PROP BETS / PROP WAGERS (unchanged) ─────────────────────────────────

-- ─── SCHEMA META (migration guard) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '7');
```

### Timestamp Policy

- **Storage:** UTC ISO 8601 via `strftime('%Y-%m-%dT%H:%M:%SZ','now')` on every connection
- **Display:** `discord.utils.format_dt(datetime.fromisoformat(ts.replace('Z','+00:00')), style='f')` — renders in each user's local timezone automatically

### event_id Assignment

| Source | Format | Primary field | Fallback trigger |
|--------|--------|---------------|-----------------|
| TSL | `tsl:{scheduleId}` | `scheduleId` from MaddenStats API (`df_games` column) | If `scheduleId` is falsy or `'0'` |
| TSL fallback | `tsl:s{season}:w{week}:{homeTeamId}v{awayTeamId}` | Composite deterministic key | — |
| REAL | `real:{sport_key}:{espn_event_id}` | ESPN event ID | N/A — always present |
| POLY | `poly:{market_id}` | Polymarket market_id | N/A — always present |

Events are written at **schedule-publication time** (bot startup or first API poll), not at bet-placement time. By the time a user places a bet, the `event_id` already exists in `events` and the FK is simply referenced.

---

## Component Responsibilities

| Module | Job | Grades? |
|--------|-----|---------|
| `sportsbook_core.py` *(new, ~450 lines)* | `flow.db` schema, `grade_bet()` pure function, `settle_event()`, settlement worker, per-event asyncio locks, `flow_events.py` bus subscription, 10-min fallback poll | **Only here** |
| `flow_sportsbook.py` | MaddenStats fetch → write `events`, TSL bet placement UI | No |
| `real_sportsbook_cog.py` | ESPN polling → write `events` + scores, real bet UI | No |
| `polymarket_cog.py` | Polymarket polling → write `events` + resolution, prediction UI | No |
| `flow_wallet.py` | Balances, transactions ledger | **Untouched** |
| `wager_registry.py` | Audit trail | **Untouched** |
| `flow_events.py` | Existing event bus | +`"event_finalized"` event type string constant |

### Code Deleted From Ingestor Cogs

**flow_sportsbook.py:** `_run_autograde`, `_fuzzy_match`, `_build_score_lookup`, `_grade_single_bet`, `_check_parlay_completion`, `_grade_bets_impl`, `auto_grade` task loop

**real_sportsbook_cog.py:** `_grade_event`, `_grade_parlay_legs_for_event`

**polymarket_cog.py:** `_auto_resolve_pass`, `_resolve`

---

## Settlement Flow

### Concurrency Protection

`sportsbook_core.py` maintains a dict `_settle_locks: dict[str, asyncio.Lock]`. Before grading any bets for an event, `settle_event()` acquires `_settle_locks[event_id]`. This prevents two concurrent calls (bus + fallback poll firing simultaneously) from double-processing.

`event_locks` table in `flow.db` is **only for bet-placement guards** (prevent new bets after game starts). Settlement concurrency is managed in-process via asyncio locks.

### Two-DB Settlement (Credit-First Pattern)

`bets` live in `flow.db`; wallet credits live in `flow_economy.db`. No cross-file transaction. Safe ordering:

```
1. grade_bet(bet, event)  →  result  [pure function, no DB]

2. If Won: flow_wallet.credit(discord_id, payout, "BET_SETTLE",
               reference_key=f"BET_{bet_id}_settled")
   ← Opens its own BEGIN IMMEDIATE on flow_economy.db
   ← Idempotent: if reference_key already exists, returns cached balance
   ← If crash here: on retry, credit is no-op, proceeds to step 3

3. BEGIN IMMEDIATE on flow.db:
       re-check bets.status == 'Pending'  ← prevents double-processing
       UPDATE bets SET status=result WHERE bet_id=?
   COMMIT
   ← If crash after step 2 but before step 3: bet stays 'Pending';
     settlement_poll() re-runs; step 2 is idempotent (no-op); step 3 completes

4. wager_registry.settle_wager("BET", str(bet_id), result.lower(), profit)
```

This ensures: at worst, a wallet credit exists without a Won status (corrected on retry). Never: Won status without wallet credit.

### Full Settlement Pseudocode

```python
# sportsbook_core.py

_settle_locks: dict[str, asyncio.Lock] = {}

async def settle_event(event_id: str) -> None:
    lock = _settle_locks.setdefault(event_id, asyncio.Lock())
    if lock.locked():
        return  # already settling this event
    async with lock:
        async with aiosqlite.connect(FLOW_DB) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            event = await db.execute_fetchone(
                "SELECT * FROM events WHERE event_id=?", (event_id,))
            if not event or event['status'] != 'final':
                return
            pending = await db.execute_fetchall(
                "SELECT * FROM bets WHERE event_id=? AND status='Pending'",
                (event_id,))

        affected_parlays = set()
        for bet in pending:
            result = grade_bet(bet, event)   # pure function

            # Step 1: credit wallet (idempotent, flow_economy.db)
            if result == 'Won':
                payout = _payout_calc(bet['wager_amount'], bet['odds'])
                await flow_wallet.credit(
                    bet['discord_id'], payout, "BET_SETTLE",
                    reference_key=f"BET_{bet['bet_id']}_settled")

            # Step 2: mark bet in flow.db
            async with aiosqlite.connect(FLOW_DB) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                row = await db.execute_fetchone(
                    "SELECT status FROM bets WHERE bet_id=?", (bet['bet_id'],))
                if row and row['status'] != 'Pending':
                    await db.rollback(); continue
                await db.execute(
                    "UPDATE bets SET status=? WHERE bet_id=?",
                    (result, bet['bet_id']))
                await db.commit()

            # Step 3: audit trail
            profit = payout - bet['wager_amount'] if result == 'Won' else -bet['wager_amount']
            await wager_registry.settle_wager(
                "BET", str(bet['bet_id']), result.lower(), profit)

            if bet['parlay_id']:
                affected_parlays.add(bet['parlay_id'])

        for parlay_id in affected_parlays:
            await _check_parlay_completion(parlay_id)

        await _post_settlement_card(event, pending)

# flow_events.py — extend with new event type
EVENT_FINALIZED = "event_finalized"  # payload: {"event_id": str, "source": str}

# Subscriber registration (sportsbook_core.py, called at cog load)
flow_bus.subscribe(EVENT_FINALIZED, lambda payload: asyncio.create_task(
    settle_event(payload["event_id"])))

# Ingestor emit pattern (all three cogs):
await flow_bus.emit(EVENT_FINALIZED, {"event_id": event_id, "source": "TSL"})

# Fallback poll
@tasks.loop(minutes=10)
async def settlement_poll():
    async with aiosqlite.connect(FLOW_DB) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        rows = await db.execute_fetchall("""
            SELECT DISTINCT e.event_id FROM events e
            JOIN bets b ON b.event_id = e.event_id
            WHERE e.status = 'final' AND b.status = 'Pending'
        """)
    for row in rows:
        await settle_event(row['event_id'])  # idempotent
```

`grade_bet(bet_row, event_row) -> 'Won'|'Lost'|'Push'` is a **pure function** — no DB access, no side effects. Handles: Moneyline, Spread, Over, Under, Prediction.

---

## Parlay Model

```
parlays → parlay_legs → bets → events
```

Cross-source parlays are natively supported. A leg is just a `bet_id` FK — it doesn't know or care which source the event came from.

```
parlay PRL_abc  (combined_odds=+650, wager=100)
  leg 0 → bet_id=1001 → event_id='tsl:447'         (TSL Madden)
  leg 1 → bet_id=1002 → event_id='real:nfl:401...' (NFL)
  leg 2 → bet_id=1003 → event_id='poly:0x9abc'     (Prediction)
```

### `_check_parlay_completion(parlay_id)` Rules

Read all legs' `bets.status` values, then apply in order:

| Leg status present | Action |
|-------------------|--------|
| Any `Lost` | Settle parlay `Lost` immediately (don't wait for remaining legs) |
| Any `Pending` (and no `Lost`) | Keep waiting |
| Any `Cancelled` (and no `Lost`, no `Pending`) | Treat `Cancelled` as `Push` for parlay purposes |
| All `Won` | Settle parlay `Won`, credit `combined_odds` payout |
| Mix of `Won` + `Push`/`Cancelled` (no `Lost`, no `Pending`) | Settle `Push`, refund `wager_amount` |

`parlay_cart` table is **kept unchanged** — it already has `source` + `event_id` columns, and its `UNIQUE(discord_id, source, event_id)` constraint is preserved (one leg per event per user per cart session).

---

## Migration: Clean Break

Guarded by `SELECT value FROM schema_meta WHERE key='schema_version'`. If `>= 7`, skip Steps 1–3.

### Step 1 — Refund All Pending Bets

```python
REFUND_SOURCES = [
    ("bets_table",            "bet_id",  "wager_amount"),
    ("real_bets",             "bet_id",  "wager_amount"),
    ("prediction_contracts",  "id",      "cost_bucks"),
    ("prop_wagers",           "id",      "wager_amount"),
]
for table, id_col, wager_col in REFUND_SOURCES:
    # Check table exists before querying (table may already be absent)
    rows = SELECT {id_col}, discord_id, {wager_col} FROM {table} WHERE status='Pending'
    for row in rows:
        await flow_wallet.credit(
            discord_id, wager_amount, "MIGRATION",
            description="Clean-break refund v7",
            reference_key=f"MIGRATE_V7_REFUND_{table}_{row_id}"
        )
        UPDATE {table} SET status='Refunded' WHERE {id_col}=?
```

### Step 2 — Archive Old Tables

```sql
ALTER TABLE bets_table           RENAME TO bets_table_arc_v7;
ALTER TABLE parlays_table        RENAME TO parlays_table_arc_v7;
ALTER TABLE parlay_legs          RENAME TO parlay_legs_arc_v7;
ALTER TABLE real_events          RENAME TO real_events_arc_v7;
ALTER TABLE real_bets            RENAME TO real_bets_arc_v7;
ALTER TABLE prediction_markets   RENAME TO prediction_markets_arc_v7;
ALTER TABLE prediction_contracts RENAME TO prediction_contracts_arc_v7;
ALTER TABLE prop_wagers          RENAME TO prop_wagers_arc_v7;
```

### Step 3 — Create New Schema

Via `sportsbook_core.setup_db()`.

### Step 4 — Seed Events

Each ingestor writes current schedule on first API poll after startup.

---

## Implementation Workstreams

| # | File | Change type | Key tasks |
|---|------|------------|-----------|
| 1 | `sportsbook_core.py` | **New** ~450 lines | `setup_db()`, `run_migration_v7()`, `grade_bet()`, `settle_event()`, `_check_parlay_completion()`, `_payout_calc()`, `settlement_poll`, bus subscription, `write_event()`, `write_bet()` public API |
| 2 | `flow_sportsbook.py` | Heavy edit | Remove 6 grading functions + task; add `_write_tsl_events(week)`; update bet/parlay placement to call `sportsbook_core.write_bet()`; emit `EVENT_FINALIZED` in score sync |
| 3 | `real_sportsbook_cog.py` | Medium edit | Remove 2 grading functions; emit `EVENT_FINALIZED` when ESPN score sync marks completed; update `_place_real_bet()` to use `sportsbook_core.write_bet()` |
| 4 | `polymarket_cog.py` | Medium edit | Remove 2 grading functions + daily settlement task; emit `EVENT_FINALIZED` when market resolved; update `register_contract()` to use `sportsbook_core.write_bet()` |
| 5 | `flow_events.py` | Light edit | Add `EVENT_FINALIZED = "event_finalized"` constant |
| 6 | `bot.py` | Light edit | Load `sportsbook_core` before all sportsbook cogs; call `run_migration_v7()` in `setup_hook()`; bump `ATLAS_VERSION` |

### Existing Functions to Reuse (Do Not Rewrite)

| Function | Current location | Move to |
|----------|-----------------|---------|
| `_payout_calc(wager, odds)` | `flow_sportsbook.py` | `sportsbook_core.py` |
| `_check_parlay_completion()` | `flow_sportsbook.py` | `sportsbook_core.py` (update table refs) |
| `flow_wallet.credit()/.debit()` | `flow_wallet.py` | Called from `sportsbook_core.settle_event()` |
| `wager_registry.settle_wager()` | `wager_registry.py` | Called from `sportsbook_core.settle_event()` |
| `ledger_poster.post_bet_settlement()` | Ledger module | Called from `sportsbook_core._post_settlement_card()` |
| `ESPNOddsClient.*` | `real_sportsbook_cog.py` | Stays in cog |
| All Discord view classes | Each cog | Stay in cog, unchanged |

**Untouched modules:** `flow_wallet.py`, `wager_registry.py`, all casino modules, `oracle_cog.py`, `echo_cog.py`, `setup_cog.py`, all renderers.

---

## Verification Checklist

- [ ] `grade_bet()` unit: all 5 bet types, win/loss/push/edge cases (tied game, exact cover, cancelled event)
- [ ] Migration: all Pending bets refunded with `MIGRATE_V7_REFUND_*` keys; all `*_arc_v7` tables exist; new schema present; `schema_version=7`
- [ ] Re-running migration: idempotent — no double-refunds, no errors
- [ ] TSL event write: `events` rows with `source='TSL'`, correct `event_id` format (`tsl:{scheduleId}` or fallback), UTC timestamps
- [ ] Bet placement FK: `bets.event_id` references valid `events` row; `PRAGMA foreign_keys=ON` enforces constraint
- [ ] Settlement bus path: manually emit `event_finalized` → bet graded → wallet credited → ledger posted
- [ ] Settlement fallback path: `final` event + pending bet + no bus event → `settlement_poll()` catches and grades
- [ ] Two-DB crash simulation: interrupt after `flow_wallet.credit()` but before `UPDATE bets`; verify retry completes correctly (no double-credit)
- [ ] Cross-source parlay: 3-leg TSL+REAL+POLY parlay stays Pending until all 3 events final, then settles
- [ ] Cancelled event parlay leg: verify parlay does not hang — `Cancelled` leg treated as `Push`
- [ ] Idempotency: `settle_event()` called twice — no double-credit, no error
- [ ] Concurrent settlement: bus + fallback poll fire simultaneously — per-event asyncio lock prevents double-processing

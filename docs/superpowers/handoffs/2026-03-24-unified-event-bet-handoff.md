# Handoff: ATLAS Unified Event-Bet Architecture (v7)

**Branch:** main
**Full spec:** `docs/superpowers/specs/2026-03-24-unified-event-bet-architecture-design.md`
**Plan file:** `C:\Users\natew\.claude\plans\mossy-hopping-kahan.md`

---

## What This Is

A ground-up redesign of the ATLAS wagering system. Three separate bet-grading pipelines
(TSL, real sports, prediction markets) are being replaced by a single unified Event-Bet
architecture. The core change: every bet is linked to a pre-written `events` table row by
foreign key. No matchup strings. No fuzzy matching. One settlement worker grades everything.

**Do not start implementing without reading the full spec first.**

---

## Decisions Already Made

| Decision | Answer |
|----------|--------|
| Sicko Mode (parallel sessions)? | No — single session |
| TSL event_id source | `scheduleId` from MaddenStats API (exists in `df_games`). Fallback: `tsl:s{season}:w{week}:{homeTeamId}v{awayTeamId}` when `scheduleId` is falsy or `'0'` |
| Architecture pattern | Shared core module (`sportsbook_core.py`) — ingestor cogs write events, core grades |
| Settlement trigger | `flow_events.py` bus event `"event_finalized"` + 10-min fallback poll loop |
| Migration strategy | Clean break — refund all Pending bets, archive old tables as `*_arc_v7`, fresh schema |
| Database name | `flow.db` (replaces `sportsbook.db`); `flow_economy.db` (wallet) untouched |
| Timestamps | UTC ISO 8601 storage; `discord.utils.format_dt()` for display (auto local TZ) |

---

## New File: `sportsbook_core.py`

This is the centerpiece. Create it from scratch. It owns:
- `flow.db` schema creation (`setup_db()`)
- One-time migration (`run_migration_v7()`) — guarded by `schema_meta.schema_version >= 7`
- `grade_bet(bet_row, event_row) -> str` — pure function, no DB calls
- `settle_event(event_id)` — the only place bets are graded
- `_check_parlay_completion(parlay_id)` — moved from `flow_sportsbook.py`
- `_payout_calc(wager, odds)` — moved from `flow_sportsbook.py`
- `settlement_poll` — `@tasks.loop(minutes=10)` fallback
- `write_event(...)` / `write_bet(...)` — public API for ingestor cogs
- Per-event asyncio locks (`_settle_locks: dict[str, asyncio.Lock]`)

Public API used by ingestor cogs:
```python
import sportsbook_core

await sportsbook_core.write_event(event_id, source, home, away, commence_ts, payload)
await sportsbook_core.write_bet(discord_id, event_id, bet_type, pick, line, odds, wager)
await sportsbook_core.settle_event(event_id)  # ingestors shouldn't call this directly
```

---

## Critical Architecture Details

### Two-Database Settlement Pattern

`bets` live in `flow.db`; wallet lives in `flow_economy.db`. SQLite has no cross-file
transactions. Use **credit-first ordering**:

```
1. grade_bet()  →  result                          [pure, no DB]
2. flow_wallet.credit(..., reference_key="BET_{bet_id}_settled")  [flow_economy.db, idempotent]
3. BEGIN IMMEDIATE on flow.db → re-check status='Pending' → UPDATE status → COMMIT
4. wager_registry.settle_wager(...)
```

If bot crashes between step 2 and 3: bet stays Pending → `settlement_poll()` retries →
step 2 is no-op (reference_key guard) → step 3 completes. Never: Won status without wallet credit.

### `PRAGMA foreign_keys=ON`

Must be set on **every connection** to `flow.db`. SQLite does NOT enforce FKs by default.
Add to `setup_db()` and to every `aiosqlite.connect()` in `sportsbook_core.py`.

### Concurrency

`sportsbook_core.py` has `_settle_locks: dict[str, asyncio.Lock]`. `settle_event()` acquires
`_settle_locks[event_id]` before doing anything. The `event_locks` table in `flow.db` is
**only for bet-placement guards** (prevent new bets after game starts). It is NOT used
for settlement concurrency.

### `flow_bus` Correct API

The bus lives in `flow_events.py` (not `flow_bus.py`). It uses a string-keyed API:

```python
# flow_events.py — add this constant
EVENT_FINALIZED = "event_finalized"

# Ingestors emit:
await flow_bus.emit(EVENT_FINALIZED, {"event_id": event_id, "source": "TSL"})

# sportsbook_core.py subscribes at cog load:
flow_bus.subscribe(EVENT_FINALIZED, lambda p: asyncio.create_task(settle_event(p["event_id"])))
```

### Parlay Cancelled Leg Rule

`_check_parlay_completion()` must handle `Cancelled` status: treat it as `Push` for
parlay purposes. Without this, a cancelled game leg leaves the parlay hanging in Pending forever.

---

## Files to Modify

| File | Change type | What to do |
|------|------------|------------|
| `sportsbook_core.py` | **Create** ~450 lines | See spec for full schema + pseudocode |
| `flow_sportsbook.py` | Heavy edit | Remove: `_run_autograde`, `_fuzzy_match`, `_build_score_lookup`, `_grade_single_bet`, `_check_parlay_completion`, `_grade_bets_impl`, `auto_grade` task. Add: `_write_tsl_events(week)`, update bet/parlay placement to call `sportsbook_core.write_bet()`, emit `EVENT_FINALIZED` in score sync |
| `real_sportsbook_cog.py` | Medium edit | Remove: `_grade_event`, `_grade_parlay_legs_for_event`. Update `sync_scores_task()` to emit `EVENT_FINALIZED` when ESPN marks completed. Update `_place_real_bet()` to use `sportsbook_core.write_bet()` |
| `polymarket_cog.py` | Medium edit | Remove: `_auto_resolve_pass`, `_resolve`, daily settlement task. Update market sync to emit `EVENT_FINALIZED` when resolved. Update `register_contract()` to use `sportsbook_core.write_bet()` |
| `flow_events.py` | Light edit | Add `EVENT_FINALIZED = "event_finalized"` constant |
| `bot.py` | Light edit | Load `sportsbook_core` before all sportsbook cogs in `setup_hook()`. Call `await sportsbook_core.run_migration_v7()` after all cogs load. Bump `ATLAS_VERSION` to next minor (6.19.0) |

**Untouched:** `flow_wallet.py`, `wager_registry.py`, all casino files, all renderers, `oracle_cog.py`, `echo_cog.py`, `setup_cog.py`

---

## `flow.db` Schema (Condensed)

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('TSL','REAL','POLY')),
    status TEXT NOT NULL DEFAULT 'scheduled'
           CHECK(status IN ('scheduled','live','final','cancelled')),
    home_participant TEXT, away_participant TEXT,
    home_score REAL, away_score REAL,
    result_payload TEXT,
    commence_ts TEXT, finalized_ts TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    bet_type TEXT NOT NULL CHECK(bet_type IN ('Moneyline','Spread','Over','Under','Prediction')),
    pick TEXT NOT NULL, line REAL, odds INTEGER NOT NULL, wager_amount INTEGER NOT NULL,
    status TEXT DEFAULT 'Pending'
           CHECK(status IN ('Pending','Won','Lost','Push','Cancelled','Error')),
    parlay_id TEXT REFERENCES parlays(parlay_id),
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE parlays (
    parlay_id TEXT PRIMARY KEY, discord_id INTEGER NOT NULL,
    combined_odds INTEGER NOT NULL, wager_amount INTEGER NOT NULL,
    status TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Won','Lost','Push','Cancelled')),
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE parlay_legs (
    leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id TEXT NOT NULL REFERENCES parlays(parlay_id),
    bet_id INTEGER NOT NULL REFERENCES bets(bet_id),
    leg_index INTEGER NOT NULL, UNIQUE(parlay_id, leg_index)
);

CREATE TABLE event_locks (event_id TEXT PRIMARY KEY REFERENCES events(event_id), locked INTEGER DEFAULT 0);
CREATE TABLE event_line_overrides (event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    home_spread REAL, away_spread REAL, home_ml INTEGER, away_ml INTEGER, ou_line REAL, set_by TEXT, set_at TEXT);
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '7');

-- Indexes
CREATE INDEX IF NOT EXISTS idx_events_source_status ON events(source, status);
CREATE INDEX IF NOT EXISTS idx_bets_event_status    ON bets(event_id, status);
CREATE INDEX IF NOT EXISTS idx_bets_user            ON bets(discord_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bets_parlay          ON bets(parlay_id) WHERE parlay_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_parlays_user_status  ON parlays(discord_id, status);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay   ON parlay_legs(parlay_id);
```

---

## Migration (`run_migration_v7`)

Refund tables in this order (check each table exists before querying):

| Table | id col | wager col |
|-------|--------|-----------|
| `bets_table` | `bet_id` | `wager_amount` |
| `real_bets` | `bet_id` | `wager_amount` |
| `prediction_contracts` | `id` | `cost_bucks` |
| `prop_wagers` | `id` | `wager_amount` |

`reference_key` format: `f"MIGRATE_V7_REFUND_{table}_{row_id}"`

Then rename all of the above + `parlays_table`, `parlay_legs`, `real_events`,
`prediction_markets` → append `_arc_v7`.

Guard: skip entire migration if `schema_meta.schema_version >= 7`.

---

## Verification Steps

Run these after implementation to confirm correctness:

1. `grade_bet()` unit test — all 5 bet types, win/loss/push/edge cases
2. Migration — Pending bets refunded, tables renamed `*_arc_v7`, `schema_version=7`
3. Re-run migration — idempotent, no errors, no double-refunds
4. TSL event write — `events` rows appear with correct `event_id` and UTC timestamps
5. Bet placement — `bets.event_id` is a valid FK; `PRAGMA foreign_keys` enforced
6. Settlement bus path — emit `event_finalized` manually → bet graded → wallet credited
7. Settlement fallback — `final` event + Pending bet + no bus → `settlement_poll()` catches it
8. Two-DB crash simulation — interrupt between wallet credit and bet UPDATE; verify clean retry
9. Cross-source parlay — 3-leg TSL+REAL+POLY settles correctly
10. Cancelled leg parlay — event cancelled → leg treated as Push → parlay settles
11. Idempotency — `settle_event()` twice → no double-credit
12. Concurrent settlement — bus + fallback poll simultaneously → asyncio lock prevents double-grade

---

## Starting Point for New Session

Suggested prompt to paste at the start of a new session:

```
I need you to implement the ATLAS Unified Event-Bet Architecture (v7).
The full design spec is at:
  docs/superpowers/specs/2026-03-24-unified-event-bet-architecture-design.md
The handoff doc is at:
  docs/superpowers/handoffs/2026-03-24-unified-event-bet-handoff.md

Read both files before writing a single line of code, then read the key
source files: flow_sportsbook.py, real_sportsbook_cog.py, polymarket_cog.py,
flow_wallet.py, flow_events.py, wager_registry.py, bot.py.

Implementation order:
1. sportsbook_core.py (new) — schema, migration, grade_bet, settle_event
2. flow_events.py — add EVENT_FINALIZED constant
3. bot.py — wire sportsbook_core, run migration, bump version
4. flow_sportsbook.py — remove grading, add event writer, update placement
5. real_sportsbook_cog.py — remove grading, update score sync + placement
6. polymarket_cog.py — remove grading, update market sync + placement

Do NOT start with the Discord UI or any visual changes. Start with the
sportsbook_core.py schema and migration, verify it works, then proceed.
```

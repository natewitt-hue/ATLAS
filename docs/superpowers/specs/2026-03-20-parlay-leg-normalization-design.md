# Parlay Leg Normalization — Design Spec

## Problem

`parlays_table.legs` stores an opaque JSON blob of leg objects. Individual leg outcomes aren't tracked — only the parlay's overall result. This makes it impossible to answer questions like "how many spread bets hit this week" or "which leg lost on parlay X" without parsing JSON in application code.

**GAP 3** from the Flow Economy audit handoff (`greedy-herding-iverson.md`).

---

## Decision Record

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary goal | Per-leg analytics | Enable SQL queries on bet type, matchup, and individual leg outcomes |
| Approach | New table + dual-write | Low risk — JSON column stays as read-only cache, new queries use relational table |
| JSON column | Keep as cache | Existing display code continues reading JSON unchanged; no atomic consumer migration needed |
| Leg status | Per-leg status column | Each leg gets its own `Pending/Won/Lost/Push` status during grading |

---

## Schema

```sql
CREATE TABLE IF NOT EXISTS parlay_legs (
    leg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id  TEXT    NOT NULL REFERENCES parlays_table(parlay_id),
    leg_index  INTEGER NOT NULL,   -- 0-based position in parlay
    game_id    TEXT    NOT NULL,
    matchup    TEXT    NOT NULL,
    pick       TEXT    NOT NULL,
    bet_type   TEXT    NOT NULL,   -- 'Spread' | 'Moneyline' | 'Over' | 'Under'
    line       REAL    NOT NULL DEFAULT 0,
    odds       INTEGER NOT NULL,   -- American odds for this leg
    status     TEXT    NOT NULL DEFAULT 'Pending',
    UNIQUE(parlay_id, leg_index)
);

CREATE INDEX IF NOT EXISTS idx_parlay_legs_parlay  ON parlay_legs(parlay_id);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_game    ON parlay_legs(game_id);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_matchup ON parlay_legs(matchup);
CREATE INDEX IF NOT EXISTS idx_parlay_legs_type    ON parlay_legs(bet_type, status);
```

### Index rationale

- **`parlay_id`** — join back to `parlays_table`, fetch all legs for a parlay
- **`game_id`** — future exact-match lookups when game ID is available
- **`matchup`** — cancellation uses case-insensitive substring match on matchup
- **`bet_type, status`** — analytics: "win rate by bet type", "how many spread bets hit this week"

### Status values

`Pending` | `Won` | `Lost` | `Push` | `Cancelled` | `Unknown` (historical backfill where per-leg result is unrecoverable)

---

## Write Path (Placement)

In `ParlayWagerModal.on_submit()`, after the existing `INSERT INTO parlays_table` and within the same `BEGIN IMMEDIATE` transaction:

```python
for i, leg in enumerate(self.legs):
    con.execute(
        "INSERT INTO parlay_legs "
        "(parlay_id, leg_index, game_id, matchup, pick, bet_type, line, odds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (parlay_id, i, leg["game_id"], leg["matchup"], leg["pick"],
         leg["bet_type"], leg.get("line", 0), leg["odds"]),
    )
```

The JSON `legs` column is still written as before — dual-write, both atomic within the same transaction.

**Data dependency:** The leg dict keys (`game_id`, `matchup`, `pick`, `bet_type`, `line`, `odds`) originate in `_make_parlay_cb()` (~line 1446) where the cart leg dict is built. No changes needed to the cart data structure — keys already match the `parlay_legs` columns.

---

## Grading Path

The existing grading loop early-breaks on the first lost leg. To record per-leg outcomes, the loop must be refactored to iterate **all** legs before determining the parlay's overall result:

```python
# Replace: for leg in legs → break-on-loss pattern
# With: iterate all legs, collect results, then determine parlay status

leg_results = []
for leg_idx, leg in enumerate(legs):
    matchup_key = leg["matchup"].lower()
    match = _fuzzy_match(matchup_key, scores)
    if not match:
        leg_results.append("Pending")
        continue

    res = _grade_single_bet(leg["bet_type"], leg["pick"], leg["line"],
                            match["home"], match["away"],
                            match["home_score"], match["away_score"])
    leg_results.append(res)

    # Record per-leg status
    con.execute(
        "UPDATE parlay_legs SET status=? WHERE parlay_id=? AND leg_index=?",
        (res, pid, leg_idx),
    )

# Derive parlay status from leg results (same logic as before)
if "Pending" in leg_results:
    continue  # not all games resolved yet
elif all(r == "Won" for r in leg_results):
    # all won → payout
elif "Lost" in leg_results:
    # any lost → parlay lost
elif all(r == "Push" for r in leg_results):
    # all pushed → refund
else:
    # mix of Won + Push → push the parlay
```

**Key behavioral change:** The grading loop no longer breaks on the first loss — it grades all legs so each gets its own status. The overall parlay outcome determination is unchanged, just computed from the collected `leg_results` list instead of mid-loop flags.

This same refactor applies to both the auto-grade path (~line 1014) and the manual grade path (~line 2206).

---

## Cancellation Path

Current code parses JSON to find parlays affected by a cancelled matchup. The existing cancellation function `_sb_cancelgame_impl()` takes a `matchup` string and does a case-insensitive substring match. With the normalized table:

```python
# Match the existing behavior: case-insensitive substring match on matchup
affected = con.execute(
    "SELECT DISTINCT parlay_id FROM parlay_legs "
    "WHERE LOWER(matchup) LIKE ? AND status='Pending'",
    (f"%{matchup_key.lower()}%",),
).fetchall()

# Also update the cancelled legs' status
con.execute(
    "UPDATE parlay_legs SET status='Cancelled' "
    "WHERE LOWER(matchup) LIKE ? AND status='Pending'",
    (f"%{matchup_key.lower()}%",),
)
```

The `idx_parlay_legs_matchup` index helps with lookups, though `LIKE` with leading wildcard may not use it. The `game_id` index is still useful for future code that has the exact game ID available.

---

## Migration (Backfill)

One-time startup function (idempotent — skips parlays already backfilled). Uses **sync sqlite3** to match the sportsbook module's established pattern. Called from `bot.py` via `await asyncio.to_thread(backfill_parlay_legs_sync)`.

```python
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
            legs = json.loads(legs_json) if isinstance(legs_json, str) else []
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
                con.commit()  # Batched commits for crash resilience
        con.commit()
    return count
```

### Historical leg status derivation

| Parlay status | Leg status | Reasoning |
|---------------|------------|-----------|
| `Won` | `Won` | All legs must have hit for parlay to win |
| `Push` | `Unknown` | Can't determine individual leg outcomes — a "Push" parlay may have Won+Won+Push legs |
| `Lost` | `Unknown` | Can't determine which leg(s) lost without replaying scores |
| `Cancelled` | `Cancelled` | Entire parlay was cancelled |
| `Pending` | `Pending` | Not yet graded |
| `Error` | `Unknown` | Error state, can't determine leg outcomes |

---

## Example Analytics Queries

Once populated, the `parlay_legs` table enables:

```sql
-- Win rate by bet type (forward-looking, excludes historical Unknown)
SELECT bet_type,
       COUNT(*) FILTER (WHERE status = 'Won') AS wins,
       COUNT(*) FILTER (WHERE status = 'Lost') AS losses,
       ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'Won') /
             NULLIF(COUNT(*) FILTER (WHERE status IN ('Won','Lost')), 0), 1) AS win_pct
FROM parlay_legs
WHERE status IN ('Won', 'Lost')
GROUP BY bet_type;

-- All legs for a specific parlay
SELECT leg_index, matchup, pick, bet_type, odds, status
FROM parlay_legs WHERE parlay_id = ? ORDER BY leg_index;

-- Which parlays have a leg on a specific game
SELECT pl.parlay_id, p.discord_id, p.wager_amount, pl.pick
FROM parlay_legs pl
JOIN parlays_table p ON p.parlay_id = pl.parlay_id
WHERE pl.game_id = ? AND pl.status = 'Pending';

-- Most popular bet types in parlays
SELECT bet_type, COUNT(*) AS leg_count
FROM parlay_legs GROUP BY bet_type ORDER BY leg_count DESC;
```

---

## Scope Boundaries

### In scope
- `parlay_legs` table creation + indexes
- Dual-write on parlay placement
- Per-leg status updates during grading
- Cancellation path optimization
- Startup backfill migration
- Schema creation in sportsbook init

### Out of scope (follow-up work)
- Migrating display consumers (`flow_cards.py`, `sportsbook_cards.py`) to read from `parlay_legs` instead of JSON
- Removing the JSON `legs` column from `parlays_table`
- Integration with `transactions` subsystem tagging (GAP 2 already covers parlay-level tagging)
- Analytics commands/UI exposing per-leg data to users

---

## Files to Modify

| File | Changes |
|------|---------|
| `flow_sportsbook.py` | Schema migration (CREATE TABLE + indexes), dual-write in `ParlayWagerModal.on_submit()`, per-leg status UPDATE in parlay grading loop, cancellation path optimization, backfill function |
| `bot.py` | Call `backfill_parlay_legs()` at startup, bump `ATLAS_VERSION` to 4.2.0 |

### Not modified
- `flow_wallet.py` — no wallet changes needed
- `sportsbook_cards.py` — continues reading JSON (out of scope)
- `flow_cards.py` — continues reading JSON (out of scope)
- `casino/renderer/highlight_renderer.py` — parlay hit card unchanged
- `flow_live_cog.py` — event triggers unchanged

---

## Verification

1. **Start bot** — verify `parlay_legs` table created (check logs)
2. **Check backfill** — `SELECT COUNT(*) FROM parlay_legs` matches total legs across all parlays
3. **Place a parlay** — verify rows in both `parlays_table.legs` (JSON) and `parlay_legs` (relational)
4. **Grade bets** — verify each leg gets individual status in `parlay_legs`
5. **Cancel a matchup** — verify cancellation finds affected parlays via `parlay_legs.game_id` index
6. **Analytics query** — run win-rate-by-bet-type query, confirm results make sense
7. **Idempotency** — restart bot, verify backfill skips already-populated parlays

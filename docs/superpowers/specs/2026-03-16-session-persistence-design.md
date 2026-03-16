# Flow Live Session Persistence

**Date:** 2026-03-16
**Status:** Draft
**Problem:** Bot restarts (multiple times daily) wipe all player session data — streaks, win/loss tallies, and profit tracking reset to zero. Players get false recap cards and the pulse dashboard goes blank.

---

## Goals

1. Player sessions survive bot restarts seamlessly — no false recaps, no lost streaks
2. Sportsbook events create/update sessions (not just casino)
3. Sessions stay alive indefinitely until the player is idle for 5 minutes with no restart in between (current behavior preserved, just persistent now)

## Non-Goals

- Historical session analytics (this is live tracking only)
- Changing the pulse dashboard or recap card rendering
- Changing highlight detection logic

---

## Design

### Database Schema

New table in `flow_economy.db`:

```sql
CREATE TABLE IF NOT EXISTS flow_live_sessions (
    discord_id     INTEGER NOT NULL,
    guild_id       INTEGER NOT NULL,
    started_at     REAL    NOT NULL,
    last_activity  REAL    NOT NULL,
    total_games    INTEGER DEFAULT 0,
    wins           INTEGER DEFAULT 0,
    losses         INTEGER DEFAULT 0,
    pushes         INTEGER DEFAULT 0,
    net_profit     INTEGER DEFAULT 0,
    biggest_win    INTEGER DEFAULT 0,
    biggest_loss   INTEGER DEFAULT 0,
    current_streak INTEGER DEFAULT 0,
    best_streak    INTEGER DEFAULT 0,
    games_by_type  TEXT    DEFAULT '{}',
    events         TEXT    DEFAULT '[]',
    PRIMARY KEY (discord_id, guild_id)
);
```

Every scalar field from `PlayerSession` maps 1:1 to a column. `games_by_type` (dict) and `events` (list of event dicts) are JSON-serialized. The composite primary key matches the existing `SessionTracker._active` dictionary key of `(discord_id, guild_id)`.

### Changes to SessionTracker

**New method: `_persist(session: PlayerSession)`**
- Called at the end of every `record()` call
- Upserts the session row using `INSERT OR REPLACE`
- Serializes `games_by_type` with `json.dumps()`
- Serializes `events` list — each event dataclass converted to dict via a `_event_to_dict()` helper

**New method: `_delete_persisted(discord_id, guild_id)`**
- Called when `collect_expired()` removes a session
- Deletes the row from `flow_live_sessions`

**New method: `load_persisted() -> int`**
- Called once during `cog_load` (before background tasks start)
- Reads all rows from `flow_live_sessions`
- Reconstructs `PlayerSession` objects from each row
- Deserializes JSON fields back into dicts/lists
- Populates `_active` dictionary
- Returns count of restored sessions (for logging)

**Modified: `record(event)`**
- After updating the session, calls `self._persist(session)`
- No other logic changes

**Modified: `collect_expired()`**
- After removing expired sessions from `_active`, calls `self._delete_persisted()` for each
- No change to expiry logic (still 5-minute idle timeout)

### Sportsbook Session Integration

**Modified: `_on_sportsbook_result(event: SportsbookEvent)`**
- Currently only checks for highlights
- New behavior: also calls `self.sessions.record_sportsbook(event)` to create/update a session
- This means sportsbook-only players appear on the pulse dashboard and get recap cards

**New method on SessionTracker: `record_sportsbook(event: SportsbookEvent) -> PlayerSession`**
- Looks up or creates session by `(event.discord_id, event.guild_id)`
- Updates `last_activity`
- Increments `total_games` by 1
- Increments `wins` or `losses` based on `event.amount` sign
- Updates `net_profit += event.amount`
- Updates `biggest_win` / `biggest_loss`
- Updates streak counters
- Increments `games_by_type["sportsbook"]`
- Does NOT append to `events` list (sportsbook events have a different shape — keep events list for casino `GameResultEvent` only)
- Calls `self._persist(session)`

### Startup Sequence

Current `cog_load`:
1. Load pulse message IDs from DB
2. Start pulse_loop
3. Start session_reaper

New `cog_load`:
1. Load pulse message IDs from DB
2. **Load persisted sessions into `_active`** ← NEW
3. **Log count of restored sessions** ← NEW
4. Start pulse_loop
5. Start session_reaper

The session reaper's first run (within 30 seconds) will naturally expire any sessions that were idle for 5+ minutes before the restart. Sessions that were recently active will stay alive and continue normally.

### Shutdown Behavior

No special shutdown hook needed. Sessions are persisted on every `record()` call, so the DB is always up to date. If the bot crashes, the worst case is losing the last in-flight event (which hasn't been persisted yet) — acceptable.

### Event Serialization

`events` list stores `GameResultEvent` objects. For JSON serialization:

```python
def _event_to_dict(event: GameResultEvent) -> dict:
    return {
        "discord_id": event.discord_id,
        "guild_id": event.guild_id,
        "game_type": event.game_type,
        "wager": event.wager,
        "outcome": event.outcome,
        "payout": event.payout,
        "multiplier": event.multiplier,
        "new_balance": event.new_balance,
        "txn_id": event.txn_id,
        "extra": event.extra,
    }

def _dict_to_event(d: dict) -> GameResultEvent:
    return GameResultEvent(**d)
```

### Write Volume Analysis

- ~30 concurrent players at peak
- ~1 game per second per active player (slots spam is fastest)
- = ~30 SQLite writes/sec peak
- SQLite WAL mode handles 1000+ writes/sec easily
- `flow_economy.db` already uses WAL mode for the casino

### Files Modified

1. **`flow_live_cog.py`** — Load persisted sessions in `cog_load`, wire sportsbook into session tracking
2. **`flow_live_cog.py` (SessionTracker class)** — Add `_persist()`, `_delete_persisted()`, `load_persisted()`, `record_sportsbook()` methods
3. **`flow_live_cog.py` (PlayerSession class)** — Add `to_dict()` / `from_dict()` class methods for serialization

No new files. No changes to renderers, highlight detection, or pulse dashboard logic.

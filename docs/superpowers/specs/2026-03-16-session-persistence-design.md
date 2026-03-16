# Flow Live Session Persistence

**Date:** 2026-03-16
**Status:** Reviewed
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

**Table creation:** An `_ensure_sessions_table()` method (following the existing `_ensure_state_table()` pattern) runs the DDL. Called at the top of `load_persisted()`.

**WAL mode:** All new `sqlite3.connect("flow_economy.db")` calls include `PRAGMA journal_mode=WAL` for defensive correctness, matching `casino_db.py` and `economy_cog.py`.

**Events list cap:** The `events` list is capped at 20 entries (FIFO). The pulse dashboard only uses the last 3 per session for highlights, so no data loss. This keeps the JSON blob small (~6KB worst case) and write latency constant.

### Changes to SessionTracker

**New method: `_persist(session: PlayerSession)`**
- Called at the end of every `record()` call
- Upserts the session row using `INSERT OR REPLACE`
- Serializes `games_by_type` with `json.dumps()`
- Serializes `events` list (capped at 20) — each event dataclass converted to dict via a `_event_to_dict()` helper
- Uses `sqlite3.connect("flow_economy.db", timeout=10)` to handle lock contention with casino writes
- Wrapped in try/except `sqlite3.OperationalError` — if DB is locked, the in-memory session is still valid and the next `record()` call will retry the persist. Never crashes the event handler.

**New method: `_delete_persisted(discord_id, guild_id)`**
- Called when `collect_expired()` removes a session
- Deletes the row from `flow_live_sessions`

**New method: `load_persisted() -> int`**
- Called once during `cog_load` (before background tasks start)
- Reads all rows from `flow_live_sessions`
- Reconstructs `PlayerSession` objects from each row via `PlayerSession.from_dict()`
- Deserializes `games_by_type` as `defaultdict(int, json.loads(...))` to preserve the auto-defaulting behavior
- Deserializes `events` list via defensive `_dict_to_event()` with `.get()` defaults (tolerates schema drift)
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
- Increments `wins`, `losses`, or `pushes` based on `event.amount` sign (positive=win, negative=loss, zero=push/void)
- Updates `net_profit += event.amount`
- Updates `biggest_win` / `biggest_loss`
- Updates streak counters (sportsbook and casino results share one streak counter — a "hot streak" is a hot streak regardless of source)
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
    # Filter extra to only JSON-safe primitive values
    safe_extra = {k: v for k, v in event.extra.items()
                  if isinstance(v, (str, int, float, bool, type(None)))}
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
        "extra": safe_extra,
    }

def _dict_to_event(d: dict) -> GameResultEvent:
    return GameResultEvent(
        discord_id=d.get("discord_id", 0),
        guild_id=d.get("guild_id", 0),
        game_type=d.get("game_type", "unknown"),
        wager=d.get("wager", 0),
        outcome=d.get("outcome", "unknown"),
        payout=d.get("payout", 0),
        multiplier=d.get("multiplier", 1.0),
        new_balance=d.get("new_balance", 0),
        txn_id=d.get("txn_id"),
        extra=d.get("extra", {}),
    )
```

### Blocking Considerations

`_persist()` uses sync `sqlite3` from an async event handler, matching the existing pattern in `flow_live_cog.py` (lines 230, 246, 257, 358). Each write is <1ms in WAL mode. If latency becomes an issue, the path forward is wrapping in `run_in_executor()` or migrating to `aiosqlite`, but this is not needed at current scale.

### Write Volume Analysis

- ~30 concurrent players at peak
- ~1 game per second per active player (slots spam is fastest)
- = ~30 SQLite writes/sec peak
- SQLite WAL mode handles 1000+ writes/sec easily
- `flow_economy.db` already uses WAL mode for the casino
- Events list capped at 20 entries keeps JSON blob ~6KB max

### Files Modified

1. **`flow_live_cog.py`** — Load persisted sessions in `cog_load`, wire sportsbook into session tracking
2. **`flow_live_cog.py` (SessionTracker class)** — Add `_persist()`, `_delete_persisted()`, `load_persisted()`, `record_sportsbook()` methods
3. **`flow_live_cog.py` (PlayerSession class)** — Add `to_dict()` / `from_dict()` class methods for serialization

No new files. No changes to renderers, highlight detection, or pulse dashboard logic.

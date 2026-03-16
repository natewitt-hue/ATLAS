# Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make flow_live player sessions survive bot restarts by persisting them to SQLite.

**Architecture:** Add a `flow_live_sessions` table to `flow_economy.db`. Upsert on every `record()` call, load all sessions on startup. Extend sportsbook events to create/update sessions.

**Tech Stack:** Python 3.14, sqlite3, json, dataclasses, discord.py

**Spec:** `docs/superpowers/specs/2026-03-16-session-persistence-design.md`

---

## File Structure

All changes are in one file:

- **Modify:** `flow_live_cog.py` — `PlayerSession`, `SessionTracker`, `FlowLiveCog`

No new files.

---

**Note:** Line numbers reference the file *before any changes*. After each task, subsequent line numbers will have shifted. Search for class/method names rather than relying on line numbers.

**Import strategy:** The existing file uses local `import sqlite3` inside each method. New code follows the same pattern — `json` and `sqlite3` are imported locally where needed. No module-level import changes.

---

## Chunk 1: Session Serialization + DB Layer

### Task 1: Add `to_dict()` and `from_dict()` to PlayerSession

**Files:**
- Modify: `flow_live_cog.py` — `PlayerSession` class and area just above it

- [ ] **Step 1: Add event serialization helpers**

Add these module-level helpers after the `SESSION_IDLE_TIMEOUT` constant (before `PlayerSession`):

```python
EVENTS_CAP = 20  # Max events stored per session

def _event_to_dict(event) -> dict:
    """Serialize a GameResultEvent to a JSON-safe dict."""
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

def _dict_to_event(d: dict):
    """Deserialize a dict back to GameResultEvent (defensive)."""
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

- [ ] **Step 2: Add `to_dict()` to PlayerSession**

Add this method to `PlayerSession` after the `record()` method:

```python
def to_dict(self) -> dict:
    return {
        "discord_id": self.discord_id,
        "guild_id": self.guild_id,
        "started_at": self.started_at,
        "last_activity": self.last_activity,
        "total_games": self.total_games,
        "wins": self.wins,
        "losses": self.losses,
        "pushes": self.pushes,
        "net_profit": self.net_profit,
        "biggest_win": self.biggest_win,
        "biggest_loss": self.biggest_loss,
        "current_streak": self.current_streak,
        "best_streak": self.best_streak,
        "games_by_type": dict(self.games_by_type),
        "events": [_event_to_dict(e) for e in self.events[-EVENTS_CAP:]],
    }
```

- [ ] **Step 3: Add `from_dict()` classmethod to PlayerSession**

```python
@classmethod
def from_dict(cls, d: dict) -> "PlayerSession":
    session = cls(
        discord_id=d["discord_id"],
        guild_id=d["guild_id"],
    )
    session.started_at = d.get("started_at", session.started_at)
    session.last_activity = d.get("last_activity", session.last_activity)
    session.total_games = d.get("total_games", 0)
    session.wins = d.get("wins", 0)
    session.losses = d.get("losses", 0)
    session.pushes = d.get("pushes", 0)
    session.net_profit = d.get("net_profit", 0)
    session.biggest_win = d.get("biggest_win", 0)
    session.biggest_loss = d.get("biggest_loss", 0)
    session.current_streak = d.get("current_streak", 0)
    session.best_streak = d.get("best_streak", 0)
    session.games_by_type = defaultdict(int, d.get("games_by_type", {}))
    session.events = [_dict_to_event(e) for e in d.get("events", [])]
    return session
```

- [ ] **Step 4: Cap events list in `record()`**

In `PlayerSession.record()`, change `self.events.append(event)` (line 57) to:

```python
self.events.append(event)
if len(self.events) > EVENTS_CAP:
    self.events = self.events[-EVENTS_CAP:]
```

- [ ] **Step 5: Commit**

```bash
git add flow_live_cog.py
git commit -m "feat(flow-live): add PlayerSession serialization + events cap"
```

---

### Task 2: Add DB persistence methods to SessionTracker

**Files:**
- Modify: `flow_live_cog.py` — `SessionTracker` class

- [ ] **Step 1: Add `_ensure_sessions_table()` method**

Add to `SessionTracker`, after `__init__`. This is the only method that sets WAL mode (it's a persistent DB-level setting, so per-call methods don't need it):

```python
def _ensure_sessions_table(self):
    try:
        import sqlite3
        conn = sqlite3.connect("flow_economy.db", timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
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
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        log.exception("Failed to create flow_live_sessions table")
```

- [ ] **Step 2: Add `_persist()` method**

```python
def _persist(self, session: PlayerSession):
    import json, sqlite3
    try:
        d = session.to_dict()
        conn = sqlite3.connect("flow_economy.db", timeout=10)
        conn.execute("""
            INSERT OR REPLACE INTO flow_live_sessions
            (discord_id, guild_id, started_at, last_activity,
             total_games, wins, losses, pushes, net_profit,
             biggest_win, biggest_loss, current_streak, best_streak,
             games_by_type, events)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            d["discord_id"], d["guild_id"], d["started_at"], d["last_activity"],
            d["total_games"], d["wins"], d["losses"], d["pushes"], d["net_profit"],
            d["biggest_win"], d["biggest_loss"], d["current_streak"], d["best_streak"],
            json.dumps(d["games_by_type"]), json.dumps(d["events"]),
        ))
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        log.warning("DB locked during session persist for %s — will retry next event", session.discord_id)
    except Exception:
        log.exception("Failed to persist session for %s", session.discord_id)
```

- [ ] **Step 3: Add `_delete_persisted()` method**

```python
def _delete_persisted(self, discord_id: int, guild_id: int):
    try:
        import sqlite3
        conn = sqlite3.connect("flow_economy.db", timeout=10)
        conn.execute(
            "DELETE FROM flow_live_sessions WHERE discord_id = ? AND guild_id = ?",
            (discord_id, guild_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        log.exception("Failed to delete persisted session for %s", discord_id)
```

- [ ] **Step 4: Add `load_persisted()` method**

Uses a single query for both column names and data. Per-row try/except so one corrupt row doesn't abort loading the rest:

```python
def load_persisted(self) -> int:
    import json, sqlite3
    self._ensure_sessions_table()
    try:
        conn = sqlite3.connect("flow_economy.db", timeout=10)
        cursor = conn.execute("SELECT * FROM flow_live_sessions")
        col_names = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        conn.close()
        count = 0
        for row in rows:
            try:
                d = dict(zip(col_names, row))
                d["games_by_type"] = json.loads(d.get("games_by_type", "{}"))
                d["events"] = json.loads(d.get("events", "[]"))
                session = PlayerSession.from_dict(d)
                self._active[(session.discord_id, session.guild_id)] = session
                count += 1
            except Exception:
                log.exception("Skipping corrupt session row: %s", row[:2])
        return count
    except Exception:
        log.exception("Failed to load persisted sessions")
        return 0
```

- [ ] **Step 5: Wire persistence into `record()` and `collect_expired()`**

In `SessionTracker.record()`, add `self._persist(session)` before the return:

```python
def record(self, event: "GameResultEvent") -> PlayerSession:
    key = (event.discord_id, event.guild_id)
    session = self._active.get(key)
    if session is None:
        session = PlayerSession(
            discord_id=event.discord_id,
            guild_id=event.guild_id,
        )
        self._active[key] = session
    session.record(event)
    self._persist(session)
    return session
```

In `SessionTracker.collect_expired()`, add delete calls:

```python
def collect_expired(self) -> list[PlayerSession]:
    now = time.time()
    expired = []
    to_remove = []
    for key, session in self._active.items():
        if now - session.last_activity > self._idle_timeout:
            expired.append(session)
            to_remove.append(key)
    for key in to_remove:
        del self._active[key]
        self._delete_persisted(*key)
    return expired
```

- [ ] **Step 6: Commit**

```bash
git add flow_live_cog.py
git commit -m "feat(flow-live): add SQLite persistence to SessionTracker"
```

---

## Chunk 2: Sportsbook Integration + Startup Loading

### Task 3: Add `record_sportsbook()` to SessionTracker

**Files:**
- Modify: `flow_live_cog.py` (SessionTracker class)

- [ ] **Step 1: Add `record_sportsbook()` method**

Add after `record()` in `SessionTracker`:

```python
def record_sportsbook(self, event: "SportsbookEvent") -> PlayerSession:
    key = (event.discord_id, event.guild_id)
    session = self._active.get(key)
    if session is None:
        session = PlayerSession(
            discord_id=event.discord_id,
            guild_id=event.guild_id,
        )
        self._active[key] = session

    session.last_activity = time.time()
    session.total_games += 1
    session.games_by_type["sportsbook"] += 1
    session.net_profit += event.amount

    if event.amount > 0:
        session.wins += 1
        if event.amount > session.biggest_win:
            session.biggest_win = event.amount
        session.current_streak = max(session.current_streak, 0) + 1
    elif event.amount < 0:
        session.losses += 1
        if event.amount < session.biggest_loss:
            session.biggest_loss = event.amount
        session.current_streak = min(session.current_streak, 0) - 1
    else:
        session.pushes += 1

    if session.current_streak > session.best_streak:
        session.best_streak = session.current_streak

    self._persist(session)
    return session
```

- [ ] **Step 2: Wire into `_on_sportsbook_result()`**

Modify the handler in `FlowLiveCog`:

```python
async def _on_sportsbook_result(self, event):
    """Handle sportsbook result: track session + detect highlights."""
    self.sessions.record_sportsbook(event)
    highlight = self.detector.check_sportsbook(event)
    if highlight and highlight.highlight_type == HighlightType.INSTANT:
        await self._post_instant_highlight(highlight, event.guild_id)
```

- [ ] **Step 3: Commit**

```bash
git add flow_live_cog.py
git commit -m "feat(flow-live): track sportsbook events in player sessions"
```

---

### Task 4: Load persisted sessions on startup

**Files:**
- Modify: `flow_live_cog.py` — `FlowLiveCog.cog_load()` method

- [ ] **Step 1: Update `cog_load()` to restore sessions**

```python
async def cog_load(self):
    self._load_pulse_message_ids()
    restored = self.sessions.load_persisted()
    if restored:
        log.info("Restored %d persisted session(s)", restored)
    self.pulse_loop.start()
    self.session_reaper.start()
```

- [ ] **Step 2: Verify the full startup flow**

Start the bot. Check logs for:
- `"Restored N persisted session(s)"` (or 0 on first run)
- No errors from `_ensure_sessions_table()`
- Pulse loop and session reaper start normally

- [ ] **Step 3: Commit**

```bash
git add flow_live_cog.py
git commit -m "feat(flow-live): load persisted sessions on startup"
```

---

### Task 5: Bump version + final verification

**Files:**
- Modify: `bot.py` (ATLAS_VERSION)

- [ ] **Step 1: Bump `ATLAS_VERSION` patch version in `bot.py`**

- [ ] **Step 2: Manual integration test**

1. Start the bot
2. Play a casino game or place a sportsbook bet
3. Check `flow_economy.db` — verify `flow_live_sessions` table has a row
4. Restart the bot
5. Check logs — should see `"Restored 1 persisted session(s)"`
6. Pulse dashboard should show the session (not blank)
7. Wait 5 minutes idle — session should expire and post an accurate recap card
8. Check `flow_live_sessions` — row should be deleted after expiry

- [ ] **Step 3: Commit**

```bash
git add bot.py flow_live_cog.py
git commit -m "feat(flow-live): session persistence — sessions survive bot restarts

Sessions are now saved to flow_economy.db on every game/bet event and
restored on startup. Sportsbook events also create/update sessions.
Events list capped at 20 entries. DB errors are non-fatal."
```

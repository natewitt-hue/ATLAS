# User Alias System — Implementation Plan

## Problem
Identity resolution fails when someone's nickname/callsign (e.g., "KG", "Breezy", "Killa") doesn't match their Discord username, PSN, or Madden DB username. The current alias map is hardcoded in `build_member_db.py`'s `MEMBERS` seed array — adding new aliases requires a code change, and they can't be added at runtime.

## Solution
A `user_aliases` table + Discord commands so commissioners and users can register name aliases on-the-fly. The existing resolution chain picks them up automatically — no code changes needed in Codex, Oracle, or any consumer.

---

## Files Touched

| File | Change |
|------|--------|
| `build_member_db.py` | Add `user_aliases` table creation, `add_alias()`, `remove_alias()`, `get_aliases_for_user()`, update `get_alias_map()` to pull from `user_aliases` |
| `commish_cog.py` | Add `/commish alias add`, `/commish alias remove`, `/commish alias list` subgroup |
| `bot.py` | Bump `ATLAS_VERSION` from `4.9.0` → `4.10.0` |

**No changes needed** in `codex_cog.py`, `oracle_cog.py`, `intelligence.py`, `roster.py`, or any other consumer — they all call `get_alias_map()` which will automatically include the new DB aliases.

---

## Step 1: Database Layer (`build_member_db.py`)

### 1a. Create `user_aliases` table

Add to `build_member_table()` right after the `tsl_members` CREATE TABLE:

```sql
CREATE TABLE IF NOT EXISTS user_aliases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  TEXT NOT NULL,          -- who this alias belongs to
    alias       TEXT NOT NULL COLLATE NOCASE,  -- the alias text (case-insensitive)
    added_by    TEXT,                   -- discord_id of who added it
    added_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(discord_id, alias)
);
```

- `discord_id` ties to `tsl_members.discord_id`
- `alias` is the text people type (e.g., "KG", "Breezy")
- `UNIQUE(discord_id, alias)` prevents duplicate registrations
- `COLLATE NOCASE` for case-insensitive uniqueness

### 1b. Add CRUD functions

```python
def add_alias(discord_id: str, alias: str, added_by: str = None, db_path: str = DB_PATH) -> tuple[bool, str]:
    """Register a new alias for a member. Returns (success, message)."""
    # Validate: alias must be 2-30 chars, no SQL injection chars
    # Check discord_id exists in tsl_members
    # Check alias isn't already claimed by a DIFFERENT user (conflict detection)
    # INSERT OR IGNORE into user_aliases
    # Return (True, "Alias 'KG' registered for username") or (False, "reason")

def remove_alias(discord_id: str, alias: str, db_path: str = DB_PATH) -> tuple[bool, str]:
    """Remove an alias. Returns (success, message)."""
    # DELETE FROM user_aliases WHERE discord_id = ? AND alias = ?
    # Return based on rowcount

def get_aliases_for_user(discord_id: str, db_path: str = DB_PATH) -> list[dict]:
    """Get all aliases for a member. Returns list of {alias, added_by, added_at}."""

def get_all_custom_aliases(db_path: str = DB_PATH) -> dict[str, str]:
    """Get all custom aliases as {alias_lower: db_username} for merging into alias map."""
    # JOIN user_aliases ON tsl_members to resolve discord_id → db_username
    # Also falls back to resolve_db_username for members without db_username yet
```

### 1c. Update `get_alias_map()`

At the end of the existing function, before `conn.close()`, merge in custom aliases:

```python
# ── Custom user aliases (from /commish alias add) ──────────────────
custom_rows = conn.execute("""
    SELECT ua.alias, m.db_username
    FROM user_aliases ua
    JOIN tsl_members m ON ua.discord_id = m.discord_id
    WHERE m.db_username IS NOT NULL
""").fetchall()
for alias_text, db_u in custom_rows:
    alias_map[alias_text.lower()] = db_u
```

Custom aliases **override** seed-data aliases if there's a conflict (last-write-wins). This is intentional — lets commissioners fix bad auto-mappings.

---

## Step 2: Discord Commands (`commish_cog.py`)

### 2a. Add `alias` subgroup to commish

```python
alias_admin = app_commands.Group(name="alias", description="Manage user aliases.", parent=commish)
```

### 2b. `/commish alias add @member <alias>`

- Commissioner picks a Discord member + types the alias
- Calls `build_member_db.add_alias()`
- Conflict detection: if alias is already claimed by someone else, show who and reject
- On success: invalidate codex identity cache (`refresh_codex_identity()`) so it's live immediately
- Ephemeral response with confirmation embed

### 2c. `/commish alias remove @member <alias>`

- Commissioner picks a member + alias to remove
- Calls `build_member_db.remove_alias()`
- Refreshes identity cache
- Ephemeral confirmation

### 2d. `/commish alias list` (optional `@member` filter)

- No member specified → show all custom aliases grouped by user (paginated if >25)
- Member specified → show that user's aliases (seed data + custom)
- Shows: alias text, who added it, when

---

## Step 3: Version Bump (`bot.py`)

```python
ATLAS_VERSION = "4.10.0"  # user alias system — runtime alias management
```

---

## Step 4: Cache Invalidation

After any alias add/remove, call `codex_cog.refresh_codex_identity()` to reload `KNOWN_USERS` and `NICKNAME_TO_USER`. This is already a public function. The intelligence module reloads its caches on demand, so no extra work needed there.

---

## What This Fixes

| Before | After |
|--------|-------|
| "Who is KG?" → fuzzy guess, maybe wrong | Commissioner runs `/commish alias add @KingGamer KG` → exact match forever |
| New member joins, goes by "Breezy" — invisible to Oracle/Codex until code deploy | `/commish alias add @NewGuy Breezy` → works in 2 seconds |
| Someone changes their PSN — alias map stale | `/commish alias remove` old + `/commish alias add` new |
| AI fallback guesses wrong name | Custom alias takes priority over fuzzy/AI guessing |

## What This Doesn't Change

- All existing hardcoded aliases in `MEMBERS` seed data still work
- `fuzzy_resolve_user()` resolution priority unchanged — nickname dict (which now includes custom aliases) is still checked first
- No changes to any cog's command surface except commish getting the new subgroup
- No new dependencies or packages needed

---

## Estimated Effort

- `build_member_db.py`: ~80 lines (table + 4 functions + alias map update)
- `commish_cog.py`: ~90 lines (subgroup + 3 commands)
- `bot.py`: 1 line (version bump)
- **Total: ~170 lines across 3 files**

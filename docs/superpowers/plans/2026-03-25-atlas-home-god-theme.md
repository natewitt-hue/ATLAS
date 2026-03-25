# ATLAS Home, GOD Module & Theme Compliance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the `/atlas` user home card, `/god` admin module, commissioner command migration to `/boss`, and Oracle theme compliance in one coordinated commit.

**Architecture:** `/atlas` becomes a personal baseball-card command (all users) rendered via a new `atlas_home_renderer.py`. The existing admin `/atlas` group is deleted and its commands split to `/boss` (commissioner) and `/god` (new GOD-role cog). Oracle gets a `theme_id` parameter to match every other renderer. `boss_cog.py` restructures from a single `/boss` command to an `app_commands.Group` so it can host `/boss sync`, `/boss clearsync`, `/boss status`.

**Tech Stack:** discord.py 2.3+, `app_commands.Group`, `atlas_html_engine.wrap_card()`, `flow_wallet.get_theme_for_render()` / `set_theme()`, `atlas_themes.THEMES`, `aiosqlite` (async DB reads), `asyncio.get_running_loop().run_in_executor()` (sync SQLite reads on thread pool).

---

## ⚠️ Architectural Decision: `/boss` Becomes a Group

Currently `/boss` is `@app_commands.command(name="boss")` — a standalone slash command. Discord forbids a command having both a direct invocation AND subcommands. To support `/boss sync` etc., **`/boss` must become an `app_commands.Group`**, and the existing hub launcher must move to `/boss hub`. This is the only spec-compliant option.

---

## File Map

| File | Change | Responsibility |
|------|--------|----------------|
| `oracle_renderer.py` | Modify | Accept `theme_id` kwarg, pass to `wrap_card()` |
| `oracle_cog.py` | Modify | Resolve `theme_id` in `_run_and_send()` |
| `permissions.py` | Modify | Add `is_god()` and `require_god()` |
| `boss_cog.py` | Modify | Convert to Group; add `sync`, `clearsync`, `status` subcommands |
| `god_cog.py` | **New** | `/god` group with `affinity` and `rebuilddb` subcommands |
| `echo_cog.py` | Modify | Remove `echostatus` registration block entirely |
| `bot.py` | Modify | Delete `atlas_group`; add `god_cog` to load order; store `bot.start_time`; bump version |
| `atlas_home_renderer.py` | **New** | Data gathering + HTML baseball card builder |
| `atlas_home_cog.py` | **New** | `/atlas` command, module buttons view, theme cycling UI |
| `test_all_renders.py` | Modify | Add home card render test |

---

## Task 1: Oracle Theme Compliance

**Files:**
- Modify: `oracle_renderer.py:315-355`
- Modify: `oracle_cog.py:2791-2802`

- [ ] **Step 1: Update `render_oracle_card()` signature**

In `oracle_renderer.py`, change line 315:

```python
# BEFORE
async def render_oracle_card(result) -> bytes:

# AFTER
async def render_oracle_card(result, *, theme_id: str | None = None) -> bytes:
```

Change line 342:

```python
# BEFORE
full_html = wrap_card(body_with_css, "")

# AFTER
full_html = wrap_card(body_with_css, "", theme_id=theme_id)
```

- [ ] **Step 2: Update `render_oracle_card_to_file()` signature**

In `oracle_renderer.py`, change line 346:

```python
# BEFORE
async def render_oracle_card_to_file(result, filename: str = "oracle_card.png"):

# AFTER
async def render_oracle_card_to_file(result, filename: str = "oracle_card.png", *, theme_id: str | None = None):
```

Change line 352:

```python
# BEFORE
png_bytes = await render_oracle_card(result)

# AFTER
png_bytes = await render_oracle_card(result, theme_id=theme_id)
```

- [ ] **Step 3: Update `_run_and_send()` in `oracle_cog.py`**

In `oracle_cog.py`, change lines 2791-2802:

```python
async def _run_and_send(interaction: discord.Interaction, coro, filename: str = "oracle.png"):
    """Run an analysis coroutine and send the result PNG card publicly."""
    try:
        from flow_wallet import get_theme_for_render
        uid = interaction.user.id
        theme_id = get_theme_for_render(uid)

        result = await coro
        disc_file = await render_oracle_card_to_file(result, filename=filename, theme_id=theme_id)
        sent = await interaction.followup.send(file=disc_file, wait=True)
        _oracle_message_ids.add(sent.id)
        _chain_roots[sent.id] = sent.id
        _oracle_msg_times[sent.id] = time.time()
    except Exception as e:
        _log.error(f"[Oracle Intel] Analysis failed: {e}\n{traceback.format_exc()}")
        await interaction.followup.send(f"❌ Analysis failed: `{e}`", ephemeral=True)
```

> Note: `get_theme_for_render` is imported locally (same pattern as `genesis_cog.py:56-58`) to keep the lazy fallback pattern. If `flow_wallet` isn't available, wrap the import in try/except and default `theme_id = None`.

- [ ] **Step 4: Verify render with `test_all_renders.py`**

```bash
cd C:\Users\natew\Desktop\discord_bot
python test_all_renders.py
```

Expected: All existing oracle card tests still pass (non-zero file size, no exception). The theme parameter defaults to `None` → same base-token rendering as before.

- [ ] **Step 5: Commit**

```bash
git add oracle_renderer.py oracle_cog.py
git commit -m "feat: wire theme_id through oracle renderer — closes theme compliance gap"
```

---

## Task 2: permissions.py — GOD Role

**Files:**
- Modify: `permissions.py`

- [ ] **Step 1: Add `GOD_ROLE_NAME` constant and `is_god()` function**

After the `TSL_OWNER_ROLE_NAME` constant (line 38), add:

```python
GOD_ROLE_NAME = "GOD"
```

After `is_tsl_owner()` (after line 95), add:

```python
async def is_god(interaction: discord.Interaction) -> bool:
    """
    Returns True if the user has the GOD role.

    GOD is above Commissioner — has all commissioner powers plus
    destructive operations (rebuilddb, affinity reset).

    DM context: only env ADMIN_USER_IDS pass.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        return interaction.user.id in ADMIN_USER_IDS

    if any(r.name == GOD_ROLE_NAME for r in member.roles):
        return True

    return False
```

- [ ] **Step 2: Add `require_god()` decorator**

After `commissioner_only()`, add:

```python
def require_god():
    """
    app_commands.check decorator that restricts a command to GOD-role users.

    Usage:
        @app_commands.command(...)
        @require_god()
        async def god_cmd(self, interaction): ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_god(interaction):
            return True
        await interaction.response.send_message(
            "ATLAS: This command requires the GOD role.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)
```

- [ ] **Step 3: Quick sanity check**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "import permissions; print('permissions OK')"
```

Expected: `permissions OK` (no import errors).

- [ ] **Step 4: Commit**

```bash
git add permissions.py
git commit -m "feat: add is_god() and require_god() to permissions"
```

---

## Task 3: boss_cog.py Restructure + Command Migration

**Files:**
- Modify: `boss_cog.py:2515-2542`

**Context:** `BossCog` currently has `@app_commands.command(name="boss")`. We need to convert it to use an `app_commands.Group(name="boss")` and add `sync`, `clearsync`, `status` as subcommands.

The sync and rebuilddb implementations live in `bot.py` as module-level async functions (`_sync_impl`, `_rebuilddb_impl`). The boss cog needs to call these — use `importlib.import_module("bot")` to get the live reference (same pattern `echo_cog.py` already uses), OR copy the logic directly. **Copy the logic directly** — it avoids the circular dependency and the spec says "move `_impl` methods to boss_cog.py."

- [ ] **Step 1: Read `boss_cog.py` to locate the class boundaries**

Use the Read tool on `boss_cog.py` offset 2510 limit 40.

You will see `class BossCog(commands.Cog):` followed by `__init__` and then `@app_commands.command(name="boss")`. **The `BossHubView` class, `_home_embed()`, all panel buttons, and every sub-panel view defined ABOVE line 2518 must be left completely untouched.** You are ONLY replacing the `BossCog` class body (the `__init__` + `boss_cmd` at the bottom of the file). Everything above `class BossCog` stays.

- [ ] **Step 2: Replace only the `BossCog` class body (not the supporting views above it)**

At the `BossCog` class (lines ~2518–2534), replace **just the class definition** — `__init__` and `boss_cmd`. All `BossHubView` panels and helper functions above the class stay intact:

```python
class BossCog(commands.Cog):
    """ATLAS Boss — Commissioner Control Room."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /boss group ──────────────────────────────────────────────────────────
    boss_group = app_commands.Group(
        name="boss",
        description="ATLAS Commissioner operations.",
        default_permissions=discord.Permissions(administrator=True),
    )

    @boss_group.command(name="hub", description="Open the ATLAS Commissioner Control Room.")
    async def boss_hub(self, interaction: discord.Interaction):
        """Launch the Commissioner Control Room hub."""
        if not await is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ This command is restricted to commissioners.", ephemeral=True,
            )
        embed = _home_embed(interaction)
        view = BossHubView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @boss_group.command(name="sync", description="Reload league data from MaddenStats API.")
    async def boss_sync(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ Commissioners only.", ephemeral=True,
            )
        await interaction.response.defer(thinking=True)
        import asyncio
        import data_manager as dm_local
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dm_local.load_all)

        try:
            import roster as roster_local
            roster_local.load()
        except Exception as e:
            log.warning(f"[Boss] Roster refresh failed: {e}")

        try:
            from build_tsl_db import sync_tsl_db
            players = dm_local.get_players()
            abilities = dm_local.get_player_abilities()
            db_result = await loop.run_in_executor(
                None, lambda: sync_tsl_db(players=players, abilities=abilities)
            )
            db_line = (
                f"\nHistory DB: **{db_result['games']}** games | "
                f"**{db_result['players']}** players ({db_result['elapsed']}s)"
                if db_result["success"]
                else f"\nHistory DB sync had issues: {', '.join(db_result['errors'][:2])}"
            )
        except Exception as e:
            db_line = f"\nHistory DB sync failed: `{e}`"

        status = dm_local.get_league_status()
        await interaction.followup.send(f"Data reloaded. League status: **{status}**{db_line}")

    @boss_group.command(name="clearsync", description="Force re-sync command tree to this server.")
    async def boss_clearsync(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ Commissioners only.", ephemeral=True,
            )
        await interaction.response.defer(thinking=True)
        self.bot.tree.clear_commands(guild=interaction.guild)
        self.bot.tree.copy_global_to(guild=interaction.guild)
        await self.bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send("Commands synced. Restart Discord to see updates.")

    @boss_group.command(name="status", description="Show ATLAS system status, uptime, and data freshness.")
    async def boss_status(self, interaction: discord.Interaction):
        if not await is_commissioner(interaction):
            return await interaction.response.send_message(
                "❌ Commissioners only.", ephemeral=True,
            )
        import time
        start_time = getattr(self.bot, "start_time", 0.0)
        uptime_sec = int(time.time() - start_time) if start_time else 0
        hours, rem = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(rem, 60)

        embed = discord.Embed(title="ATLAS System Status", color=GOLD)
        try:
            import bot as bot_mod
            embed.add_field(name="Version", value=f"v{bot_mod.ATLAS_VERSION}", inline=True)
        except Exception:
            pass
        embed.add_field(name="Uptime", value=f"{hours}h {minutes}m {seconds}s", inline=True)
        try:
            import data_manager as dm_local
            embed.add_field(name="League", value=dm_local.get_league_status(), inline=True)
        except Exception:
            pass
        cog_list = ", ".join(self.bot.cogs.keys()) or "None"
        embed.add_field(name="Cogs Loaded", value=cog_list, inline=False)
        embed.add_field(name="Guilds", value=str(len(self.bot.guilds)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
```

> **Note on `boss_status` uptime:** `bot.start_time` must be set in `bot.py`'s `on_ready()` — see Task 5.

- [ ] **Step 3: Import check**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "import boss_cog; print('boss_cog OK')"
```

Expected: `boss_cog OK`.

- [ ] **Step 4: Commit**

```bash
git add boss_cog.py
git commit -m "feat: convert /boss to group; add sync, clearsync, status subcommands"
```

---

## Task 4: god_cog.py — New GOD Module

**Files:**
- Create: `god_cog.py`

**Context:** Mirrors the structure of other cogs. Uses `require_god()` from `permissions.py`. Moves `/atlas affinity` and `/atlas rebuilddb` logic here.

The `affinity` logic is currently inline at `bot.py:714-735` and uses module-level `affinity_mod` (imported in bot.py). In god_cog we import `affinity` directly. The `rebuilddb` logic is `_rebuilddb_impl()` in `bot.py:797-823`.

- [ ] **Step 1: Create `god_cog.py`**

```python
"""
god_cog.py — ATLAS GOD-Tier Administration (/god)
─────────────────────────────────────────────────────────────────────────────
Destructive and privileged operations gated behind the "GOD" Discord role.

Role hierarchy: GOD → Commissioner → TSL Owner → User

Commands:
    /god affinity <user> [reset]  — view or reset a user's affinity score
    /god rebuilddb                — force full tsl_history.db rebuild
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_god, require_god

log = logging.getLogger("atlas.god")

# ── Optional dependency: affinity ────────────────────────────────────────────

try:
    import affinity as affinity_mod
    _AFFINITY_AVAILABLE = True
except ImportError:
    affinity_mod = None
    _AFFINITY_AVAILABLE = False

# ── /god Group ────────────────────────────────────────────────────────────────


class GodCog(commands.Cog):
    """ATLAS GOD — privileged administration tier."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    god_group = app_commands.Group(
        name="god",
        description="ATLAS God-tier administration.",
        default_permissions=discord.Permissions(administrator=True),
    )

    @god_group.command(name="affinity", description="View or reset a user's ATLAS affinity score.")
    @app_commands.describe(user="The user to check", reset="Reset their score to 0?")
    async def god_affinity(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reset: bool = False,
    ):
        if not await is_god(interaction):
            return await interaction.response.send_message(
                "ATLAS: This command requires the GOD role.", ephemeral=True,
            )
        if not _AFFINITY_AVAILABLE:
            return await interaction.response.send_message(
                "❌ Affinity system not loaded.", ephemeral=True,
            )
        if reset:
            await affinity_mod.reset_affinity(user.id)
            await interaction.response.send_message(
                f"🔄 Reset affinity for **{user.display_name}** to 0.", ephemeral=True,
            )
        else:
            score = await affinity_mod.get_affinity(user.id)
            tier = affinity_mod.get_tier_label(score)
            embed = discord.Embed(
                title=f"User Affinity — {user.display_name}",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Score", value=f"`{score:.1f}`", inline=True)
            embed.add_field(name="Tier", value=tier, inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @god_group.command(name="rebuilddb", description="Force full rebuild of tsl_history.db.")
    async def god_rebuilddb(self, interaction: discord.Interaction):
        if not await is_god(interaction):
            return await interaction.response.send_message(
                "ATLAS: This command requires the GOD role.", ephemeral=True,
            )
        await interaction.response.defer(thinking=True)
        loop = asyncio.get_running_loop()
        try:
            import data_manager as dm
            import build_tsl_db as db_builder
            players = dm.get_players()
            abilities = dm.get_player_abilities()
            db_result = await loop.run_in_executor(
                None,
                lambda: db_builder.sync_tsl_db(players=players, abilities=abilities),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ DB rebuild failed: `{e}`")
            return

        if db_result["success"]:
            lines = [
                f"**tsl_history.db rebuilt** in {db_result['elapsed']}s",
                f"Games: **{db_result['games']}**",
                f"Players: **{db_result['players']}**",
            ]
            if db_result.get("errors"):
                lines.append(f"Warnings: {', '.join(db_result['errors'][:3])}")
        else:
            lines = [f"DB rebuild failed: {', '.join(db_result['errors'][:3])}"]

        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(GodCog(bot))
        print("ATLAS: GOD · Privileged Administration loaded. ⚡")
    except Exception as e:
        print(f"ATLAS: GOD · FAILED to load ({e})")
```

- [ ] **Step 2: Import check**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "import god_cog; print('god_cog OK')"
```

Expected: `god_cog OK`.

- [ ] **Step 3: Commit**

```bash
git add god_cog.py
git commit -m "feat: add god_cog.py with /god affinity and /god rebuilddb"
```

---

## Task 5: bot.py + echo_cog.py Cleanup

**Files:**
- Modify: `bot.py:649-823` (atlas_group block)
- Modify: `bot.py:514` (on_ready — add `bot.start_time`)
- Modify: `echo_cog.py:46-62` (echostatus registration block)

**Critical:** Remove the entire `atlas_group` block AFTER confirming that `_sync_impl` and `_rebuilddb_impl` logic has been transplanted to boss_cog/god_cog in Tasks 3 & 4.

- [ ] **Step 1: Add `bot.start_time` in `on_ready()`**

In `bot.py`, in `on_ready()` after `_bot_start_time = time.time()` (line 521), add:

```python
    bot.start_time = time.time()  # Accessible to cogs via interaction.client.start_time
```

> This replaces the module-level `_bot_start_time` used by `/atlas status`. Boss cog reads `self.bot.start_time` (Task 3). Both can coexist during the transition.

- [ ] **Step 2: Remove `atlas_group` and all its commands from `bot.py`**

Delete the block from `# ── /atlas Admin Group` (line 647) through `bot.tree.add_command(atlas_group)` (line 738), inclusive. That includes:
- `atlas_group = app_commands.Group(...)` (lines 649-653)
- `_bot_start_time: float = 0.0` (line 655) — **delete this line too**; boss_cog now reads `self.bot.start_time` set in Step 1
- All `@atlas_group.command` decorated functions (lines 658-736)
- `bot.tree.add_command(atlas_group)` (line 738)

- [ ] **Step 3: Remove `_sync_impl` and `_rebuilddb_impl` from `bot.py`**

Delete lines 743-823 (`_sync_impl` and `_rebuilddb_impl` functions). Verify no other code in `bot.py` calls these functions (grep: `_sync_impl|_rebuilddb_impl`).

- [ ] **Step 4: Remove echostatus registration from `echo_cog.py`**

In `echo_cog.py`, in `setup()`, remove the entire block that registers echostatus:

```python
# DELETE from echo_cog.py setup():
    # Register echo commands on the /atlas group from bot.py
    import importlib
    bot_module = importlib.import_module("bot")
    atlas_group = bot_module.atlas_group

    @atlas_group.command(name="echostatus", description="Check current Echo persona status.")
    async def atlas_echostatus(interaction: discord.Interaction):
        cog = bot.get_cog("EchoCog")
        if cog:
            await cog._echostatus_impl(interaction)
        else:
            await interaction.response.send_message("Echo cog not loaded.", ephemeral=True)
```

Also delete `_echostatus_impl()` from `EchoCog` (lines 24-43) — it is only called from the registration block being deleted.

Update `echo_cog.py` docstring (line 6): remove the `/atlas echostatus` reference.

- [ ] **Step 5: Add `god_cog` to load order in `bot.py`**

In `_EXTENSIONS` list (line 261), add after `"boss_cog"`:

```python
        "god_cog",            # ATLAS GOD — privileged administration (/god)
```

- [ ] **Step 6: Bump `ATLAS_VERSION`**

Line 175, change minor version:

```python
ATLAS_VERSION = "7.6.0"  # feat: /atlas user home, /god module, admin migration, Oracle theme fix
```

- [ ] **Step 7: Import checks**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "import bot; print('bot.py OK')"
python -c "import echo_cog; print('echo_cog OK')"
```

Expected: both print OK without `AttributeError: module 'bot' has no attribute 'atlas_group'`.

- [ ] **Step 8: Commit**

```bash
git add bot.py echo_cog.py
git commit -m "feat: remove /atlas admin group; add god_cog to load order; v7.6.0"
```

---

## Task 6: atlas_home_renderer.py — Baseball Card Renderer

**Files:**
- Create: `atlas_home_renderer.py`

**Context:** Data gathering uses synchronous `sqlite3` inside `run_in_executor` (same pattern as `flow_wallet.py`). The HTML card uses `wrap_card()` from `atlas_html_engine.py` for theme support. DB path from `flow_wallet.DB_PATH`.

Before implementing, **check `flow_economy.db` schema** — confirm table names and column names:

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "
import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath('.')), 'discord_bot', 'flow_economy.db')
con = sqlite3.connect(db)
for row in con.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall():
    print(row[0])
    for col in con.execute(f'PRAGMA table_info({row[0]})').fetchall():
        print('  ', col[1], col[2])
"
```

Use the actual column names discovered above in the data gathering queries below.

- [ ] **Step 1: Create `atlas_home_renderer.py`**

```python
"""
atlas_home_renderer.py — ATLAS User Home Baseball Card
──────────────────────────────────────────────────────
Renders a personalized PNG "baseball card" for /atlas.

Data gathering:
  - gather_home_data(user_id) → dict  (sync, runs in executor)
  - render_home_card(data, theme_id) → bytes  (async, via render_card)

Pipeline: gather_home_data → build_home_html → wrap_card → render_card → PNG
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from atlas_html_engine import esc, render_card, wrap_card

# ── DB path (same as flow_wallet.py) ─────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
_DB_TIMEOUT = 10


# ── Data Gathering ────────────────────────────────────────────────────────────


def gather_home_data(user_id: int) -> dict:
    """
    Gather all stats needed for the home card from flow_economy.db.
    Runs synchronously — call via run_in_executor.

    Returns a dict with keys matching the card layout sections.
    All values default to safe "N/A" / 0 if data is unavailable.
    """
    data: dict = {
        "user_id": user_id,
        # Hero
        "display_name": "Unknown",
        "role_badge": "",
        "rank": 0,
        "total_users": 0,
        "balance": 0,
        "weekly_delta": 0,
        "season_roi": 0.0,
        "streak": "—",
        # Economy
        "record_w": 0,
        "record_l": 0,
        "record_p": 0,
        "win_rate": 0.0,
        "net_pnl": 0,
        # Sportsbook
        "tsl_bet_w": 0,
        "tsl_bet_l": 0,
        "best_parlay_odds": 0.0,
        "real_bet_w": 0,
        "real_bet_l": 0,
        # Casino
        "casino_sessions": 0,
        "biggest_win": 0,
        "fav_game": "—",
        # Predictions
        "pred_accuracy": 0.0,
        "pred_markets": 0,
        "pred_pnl": 0,
        # Footer
        "theme_name": "Obsidian Gold",
        "season": 0,
    }

    try:
        con = sqlite3.connect(_DB_PATH, timeout=_DB_TIMEOUT)

        # Hero — balance, rank, season_start_balance
        row = con.execute(
            "SELECT balance, season_start_balance FROM users_table WHERE discord_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            balance, season_start = row
            data["balance"] = int(balance or 0)
            if season_start and season_start > 0:
                pnl = balance - season_start
                data["season_roi"] = round(pnl / season_start * 100, 1)
                data["net_pnl"] = int(pnl)

        # Rank
        ranks = con.execute(
            "SELECT discord_id FROM users_table ORDER BY balance DESC"
        ).fetchall()
        data["total_users"] = len(ranks)
        for i, (uid,) in enumerate(ranks, 1):
            if uid == user_id:
                data["rank"] = i
                break

        # Weekly delta — last 7 days of transactions
        try:
            delta_row = con.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0)
                     - COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 0)
                FROM transactions
                WHERE discord_id = ?
                  AND created_at >= datetime('now', '-7 days')
                """,
                (user_id,),
            ).fetchone()
            if delta_row:
                data["weekly_delta"] = int(delta_row[0] or 0)
        except Exception:
            pass

        # TSL Sportsbook bets
        try:
            tsl_rows = con.execute(
                """
                SELECT result FROM bets_table
                WHERE discord_id = ? AND subsystem = 'tsl_sportsbook'
                """,
                (user_id,),
            ).fetchall()
            for (result,) in tsl_rows:
                if result == "win":
                    data["tsl_bet_w"] += 1
                elif result in ("loss", "lose"):
                    data["tsl_bet_l"] += 1
        except Exception:
            pass

        # Real sports bets
        try:
            real_rows = con.execute(
                """
                SELECT result FROM bets_table
                WHERE discord_id = ? AND subsystem = 'real_sportsbook'
                """,
                (user_id,),
            ).fetchall()
            for (result,) in real_rows:
                if result == "win":
                    data["real_bet_w"] += 1
                elif result in ("loss", "lose"):
                    data["real_bet_l"] += 1
        except Exception:
            pass

        # Casino sessions + biggest win + favorite game
        try:
            sess_row = con.execute(
                "SELECT COUNT(*) FROM casino_sessions WHERE discord_id = ?",
                (user_id,),
            ).fetchone()
            if sess_row:
                data["casino_sessions"] = int(sess_row[0] or 0)

            win_row = con.execute(
                """
                SELECT MAX(payout - amount) FROM bets_table
                WHERE discord_id = ? AND subsystem = 'casino' AND result = 'win'
                """,
                (user_id,),
            ).fetchone()
            if win_row and win_row[0]:
                data["biggest_win"] = int(win_row[0])

            fav_row = con.execute(
                """
                SELECT game_type, COUNT(*) AS cnt FROM casino_sessions
                WHERE discord_id = ?
                GROUP BY game_type ORDER BY cnt DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if fav_row:
                data["fav_game"] = str(fav_row[0]).capitalize()
        except Exception:
            pass

        # Predictions
        try:
            pred_rows = con.execute(
                """
                SELECT resolution, amount FROM prediction_positions
                WHERE discord_id = ?
                """,
                (user_id,),
            ).fetchall()
            if pred_rows:
                data["pred_markets"] = len(pred_rows)
                wins = sum(1 for r, _ in pred_rows if r == "win")
                data["pred_accuracy"] = round(wins / len(pred_rows) * 100, 1)
                data["pred_pnl"] = sum(a for _, a in pred_rows if a)
        except Exception:
            pass

        # Economy W/L/P record
        total = data["tsl_bet_w"] + data["tsl_bet_l"] + data["real_bet_w"] + data["real_bet_l"]
        wins = data["tsl_bet_w"] + data["real_bet_w"]
        data["record_w"] = wins
        data["record_l"] = data["tsl_bet_l"] + data["real_bet_l"]
        if total > 0:
            data["win_rate"] = round(wins / total * 100, 1)

        # Streak — last N resolved bets ordered by created_at
        try:
            streak_rows = con.execute(
                """
                SELECT result FROM bets_table
                WHERE discord_id = ? AND result IN ('win', 'loss', 'lose')
                ORDER BY created_at DESC LIMIT 20
                """,
                (user_id,),
            ).fetchall()
            if streak_rows:
                first = streak_rows[0][0]
                is_win = first == "win"
                count = 0
                for (r,) in streak_rows:
                    if (r == "win") == is_win:
                        count += 1
                    else:
                        break
                data["streak"] = f'W{count}' if is_win else f'L{count}'
        except Exception:
            pass  # streak stays "—"

        # Best parlay odds
        try:
            parlay_row = con.execute(
                """
                SELECT MAX(payout / CAST(amount AS FLOAT))
                FROM bets_table
                WHERE discord_id = ? AND bet_type = 'parlay' AND result = 'win' AND amount > 0
                """,
                (user_id,),
            ).fetchone()
            if parlay_row and parlay_row[0]:
                data["best_parlay_odds"] = round(parlay_row[0], 2)
        except Exception:
            pass  # best_parlay_odds stays 0.0 — rendered as "—" in _stat_cell

        con.close()
    except Exception:
        pass  # Return whatever was gathered — graceful degradation

    return data


def _stat_cell(label: str, value: str, accent: str = "#d4a843") -> str:
    """One cell in a 3-col stat grid."""
    return (
        f'<div style="background:rgba(255,255,255,0.04);border-radius:8px;'
        f'padding:10px 8px;text-align:center;">'
        f'<div style="font-size:9px;font-weight:700;color:{accent};'
        f'text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">'
        f"{esc(label)}</div>"
        f'<div style="font-size:16px;font-weight:800;color:#fff;">'
        f"{esc(str(value))}</div>"
        f"</div>"
    )


def _section(title: str, cells_html: str, accent: str = "#d4a843") -> str:
    """A labeled section with a 3-col stat grid."""
    return (
        f'<div style="margin-bottom:14px;">'
        f'<div style="font-size:10px;font-weight:700;color:{accent};'
        f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;'
        f'padding-bottom:4px;border-bottom:1px solid {accent}33;">'
        f"{esc(title)}</div>"
        f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">'
        f"{cells_html}"
        f"</div>"
        f"</div>"
    )


def _build_home_html(data: dict) -> str:
    """Build the inner HTML body for the baseball card."""
    accent = "#d4a843"
    balance_sign = "+" if data["weekly_delta"] >= 0 else ""
    roi_sign = "+" if data["season_roi"] >= 0 else ""

    hero = (
        f'<div style="padding:20px;text-align:center;'
        f'background:linear-gradient(135deg,rgba(0,0,0,0.4),rgba(20,20,30,0.9));">'
        f'<div style="font-size:22px;font-weight:900;color:#fff;'
        f'letter-spacing:-0.5px;">{esc(data["display_name"])}</div>'
        + (
            f'<div style="display:inline-block;background:{accent}22;border:1px solid {accent}44;'
            f'border-radius:12px;padding:2px 12px;font-size:10px;font-weight:700;'
            f'color:{accent};margin-top:4px;">{esc(data["role_badge"])}</div>'
            if data["role_badge"] else ""
        )
        + f'<div style="margin-top:10px;display:grid;grid-template-columns:1fr 1fr 1fr 1fr;'
        f'gap:8px;max-width:500px;margin-left:auto;margin-right:auto;">'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Rank</div>'
        f'<div style="font-size:18px;font-weight:900;color:{accent};">#{data["rank"]}</div>'
        f'<div style="font-size:9px;color:#666;">of {data["total_users"]}</div></div>'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Balance</div>'
        f'<div style="font-size:18px;font-weight:900;color:#fff;">{data["balance"]:,}</div>'
        f'<div style="font-size:9px;color:{"#4caf50" if data["weekly_delta"] >= 0 else "#f44336"};">'
        f'{balance_sign}{data["weekly_delta"]:,} wk</div></div>'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Season ROI</div>'
        f'<div style="font-size:18px;font-weight:900;'
        f'color:{"#4caf50" if data["season_roi"] >= 0 else "#f44336"};">'
        f'{roi_sign}{data["season_roi"]}%</div></div>'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Streak</div>'
        f'<div style="font-size:18px;font-weight:900;color:#fff;">{esc(data["streak"])}</div>'
        f"</div></div></div>"
    )

    economy = _section("Economy", (
        _stat_cell("Record", f'{data["record_w"]}-{data["record_l"]}-{data["record_p"]}', accent)
        + _stat_cell("Win Rate", f'{data["win_rate"]}%', accent)
        + _stat_cell("Net P&L", f'{data["net_pnl"]:+,}', accent)
    ), accent)

    sportsbook = _section("Sportsbook", (
        _stat_cell("TSL Bets", f'{data["tsl_bet_w"]}-{data["tsl_bet_l"]}', accent)
        + _stat_cell("Best Parlay", f'{data["best_parlay_odds"]}x' if data["best_parlay_odds"] else "—", accent)
        + _stat_cell("Real Sports", f'{data["real_bet_w"]}-{data["real_bet_l"]}', accent)
    ), accent)

    casino = _section("Casino", (
        _stat_cell("Sessions", str(data["casino_sessions"]), accent)
        + _stat_cell("Biggest Win", f'{data["biggest_win"]:,}', accent)
        + _stat_cell("Fav Game", data["fav_game"], accent)
    ), accent)

    predictions = _section("Predictions", (
        _stat_cell("Accuracy", f'{data["pred_accuracy"]}%', accent)
        + _stat_cell("Markets", str(data["pred_markets"]), accent)
        + _stat_cell("Pred P&L", f'{data["pred_pnl"]:+,}', accent)
    ), accent)

    footer = (
        f'<div style="text-align:center;padding:10px;font-size:9px;color:#555;">'
        f'ATLAS™ · {esc(data["theme_name"])} · Season {data["season"]}'
        f"</div>"
    )

    body = (
        f"{hero}"
        f'<div style="padding:16px 20px 0;">'
        f"{economy}{sportsbook}{casino}{predictions}"
        f"</div>"
        f"{footer}"
    )
    return body


async def render_home_card(data: dict, *, theme_id: str | None = None) -> bytes:
    """Render the home card to PNG bytes."""
    body_html = _build_home_html(data)
    full_html = wrap_card(body_html, "", theme_id=theme_id)
    return await render_card(full_html)


async def render_home_card_to_file(data: dict, *, theme_id: str | None = None, filename: str = "atlas_home.png"):
    """Render and return a discord.File."""
    import io
    import discord
    png_bytes = await render_home_card(data, theme_id=theme_id)
    buf = io.BytesIO(png_bytes)
    buf.seek(0)
    return discord.File(buf, filename=filename)
```

- [ ] **Step 2: Verify DB schema — adjust column names if needed**

Run the schema inspection command from the preamble and check:
- `users_table` columns: `discord_id`, `balance`, `season_start_balance`
- `transactions` table: `discord_id`, `amount`, `created_at`
- `bets_table` columns: `discord_id`, `subsystem`, `result`, `amount`, `payout`
- `casino_sessions` columns: `discord_id`, `game_type`
- `prediction_positions` columns: `discord_id`, `resolution`, `amount`

If any table or column doesn't exist, wrap the relevant query block in a try/except (already done for most) and accept graceful zero defaults.

- [ ] **Step 3: Add render test to `test_all_renders.py`**

Find the `test_card` calls at the bottom of `test_all_renders.py` and add:

```python
    # Atlas Home Card
    from atlas_home_renderer import render_home_card
    sample_home_data = {
        "user_id": 123456,
        "display_name": "TestUser",
        "role_badge": "TSL Owner",
        "rank": 3, "total_users": 31,
        "balance": 2400, "weekly_delta": 150,
        "season_roi": 14.2, "streak": "W3",
        "record_w": 18, "record_l": 9, "record_p": 0,
        "win_rate": 66.7, "net_pnl": 400,
        "tsl_bet_w": 10, "tsl_bet_l": 5,
        "best_parlay_odds": 4.5,
        "real_bet_w": 8, "real_bet_l": 4,
        "casino_sessions": 22, "biggest_win": 850, "fav_game": "Blackjack",
        "pred_accuracy": 71.4, "pred_markets": 7, "pred_pnl": 320,
        "theme_name": "Obsidian Gold", "season": 95,
    }
    await test_card("atlas_home", render_home_card(sample_home_data))
```

Run: `python test_all_renders.py`
Expected: `atlas_home` passes with non-zero PNG size.

- [ ] **Step 4: Commit**

```bash
git add atlas_home_renderer.py test_all_renders.py
git commit -m "feat: add atlas_home_renderer.py — baseball card data gathering + HTML builder"
```

---

## Task 7: atlas_home_cog.py — The `/atlas` Command

**Files:**
- Create: `atlas_home_cog.py`
- Modify: `bot.py` (add `"atlas_home_cog"` to `_EXTENSIONS` before `echo_cog` position 15 — anywhere after `boss_cog` is fine)

**Context:** `/atlas` is now available as a top-level command since `atlas_group` was deleted (Task 5). The cog registers it as a plain `@app_commands.command`. The theme cycling UI uses `discord.ui.View` with temporary state stored on the View instance (no DB write until "Apply").

- [ ] **Step 1: Create `atlas_home_cog.py`**

```python
"""
atlas_home_cog.py — ATLAS User Home (/atlas)
─────────────────────────────────────────────────────────────────────────────
Personal "baseball card" command for every user.

    /atlas  →  ephemeral PNG card + module navigation buttons + theme cycling

No admin access required. Card is always ephemeral.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from atlas_home_renderer import gather_home_data, render_home_card_to_file
from atlas_themes import THEMES

log = logging.getLogger("atlas.home")

# ── Module info embeds ────────────────────────────────────────────────────────

_MODULE_INFO = {
    "oracle": (
        "🔮 Oracle Intelligence",
        "AI-powered stats analysis, power rankings, matchup breakdowns, and scouting.\n"
        "Use `/oracle` to open the hub.",
    ),
    "genesis": (
        "⚔️ Genesis Trade Center",
        "Propose and approve trades, check parity ratings, and manage dev traits.\n"
        "Use `/trade` to open the hub.",
    ),
    "flow": (
        "💰 Flow Economy",
        "View your wallet, bet history, and league economy stats.\n"
        "Use `/flow` to open the hub.",
    ),
    "sportsbook": (
        "🏈 TSL Sportsbook",
        "Bet on TSL game outcomes, build parlays, and track your record.\n"
        "Use `/sportsbook` to open the hub.",
    ),
    "casino": (
        "🎰 ATLAS Casino",
        "Blackjack, Slots, Crash, and Coinflip — all under `/casino`.",
    ),
    "predictions": (
        "📈 Prediction Markets",
        "Trade YES/NO on custom markets for TSL outcomes.\n"
        "Use `/predictions` to open the hub.",
    ),
}


# ── Views ─────────────────────────────────────────────────────────────────────

class HomeView(discord.ui.View):
    """Main navigation view attached to the home card."""

    def __init__(self, user_id: int, data: dict, theme_id: Optional[str]):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.data = data
        self.theme_id = theme_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This card belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔮 Oracle", style=discord.ButtonStyle.secondary)
    async def oracle_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "oracle")

    @discord.ui.button(label="⚔️ Genesis", style=discord.ButtonStyle.secondary)
    async def genesis_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "genesis")

    @discord.ui.button(label="💰 Flow", style=discord.ButtonStyle.secondary)
    async def flow_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "flow")

    @discord.ui.button(label="🏈 Sportsbook", style=discord.ButtonStyle.secondary)
    async def sportsbook_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "sportsbook")

    @discord.ui.button(label="🎰 Casino", style=discord.ButtonStyle.secondary)
    async def casino_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "casino")

    @discord.ui.button(label="📈 Predictions", style=discord.ButtonStyle.secondary)
    async def predictions_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await _send_module_info(interaction, "predictions")

    @discord.ui.button(label="🎨 Theme", style=discord.ButtonStyle.primary)
    async def theme_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        theme_keys = list(THEMES.keys())
        current_idx = theme_keys.index(self.theme_id) if self.theme_id in theme_keys else 0
        theme_view = ThemeCycleView(
            user_id=self.user_id,
            data=self.data,
            theme_keys=theme_keys,
            current_idx=current_idx,
            original_theme=self.theme_id,
            original_message=interaction.message,
        )
        await interaction.response.defer()
        # Re-render with current (unchanged) theme to show cycling UI
        disc_file = await render_home_card_to_file(
            self.data, theme_id=self.theme_id, filename="atlas_home.png"
        )
        await interaction.edit_original_response(attachments=[disc_file], view=theme_view)


class ThemeCycleView(discord.ui.View):
    """Theme preview cycling view."""

    def __init__(
        self,
        user_id: int,
        data: dict,
        theme_keys: list[str],
        current_idx: int,
        original_theme: Optional[str],
        original_message: discord.Message,
    ):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.data = data
        self.theme_keys = theme_keys
        self.current_idx = current_idx
        self.original_theme = original_theme
        self.original_message = original_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This card belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def _current_theme_id(self) -> str:
        return self.theme_keys[self.current_idx]

    async def _re_render(self, interaction: discord.Interaction):
        await interaction.response.defer()
        theme_id = self._current_theme_id()
        disc_file = await render_home_card_to_file(
            self.data, theme_id=theme_id, filename="atlas_home.png"
        )
        await interaction.edit_original_response(attachments=[disc_file], view=self)

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_idx = (self.current_idx - 1) % len(self.theme_keys)
        await self._re_render(interaction)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_idx = (self.current_idx + 1) % len(self.theme_keys)
        await self._re_render(interaction)

    @discord.ui.button(label="✅ Apply", style=discord.ButtonStyle.success)
    async def apply_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        new_theme = self._current_theme_id()
        from flow_wallet import set_theme
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, set_theme, self.user_id, new_theme)
        theme_display = THEMES[new_theme].get("name", new_theme) if new_theme in THEMES else new_theme
        # Return to home view with updated theme
        home_view = HomeView(self.user_id, self.data, new_theme)
        disc_file = await render_home_card_to_file(
            self.data, theme_id=new_theme, filename="atlas_home.png"
        )
        await interaction.response.defer()
        await interaction.edit_original_response(
            content=f"Theme set to **{theme_display}**.",
            attachments=[disc_file],
            view=home_view,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        home_view = HomeView(self.user_id, self.data, self.original_theme)
        disc_file = await render_home_card_to_file(
            self.data, theme_id=self.original_theme, filename="atlas_home.png"
        )
        await interaction.response.defer()
        await interaction.edit_original_response(
            content=None,
            attachments=[disc_file],
            view=home_view,
        )


async def _send_module_info(interaction: discord.Interaction, key: str):
    title, desc = _MODULE_INFO[key]
    embed = discord.Embed(title=title, description=desc, color=0xd4a843)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────


class AtlasHomeCog(commands.Cog):
    """ATLAS User Home — /atlas baseball card."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="atlas", description="View your ATLAS stats card and navigate to any module.")
    async def atlas_home(self, interaction: discord.Interaction):
        """Render the user's personal ATLAS baseball card."""
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        # Gather role badge
        role_badge = ""
        if isinstance(user, discord.Member):
            for role_name in ("GOD", "Commissioner", "TSL Owner"):
                if any(r.name == role_name for r in user.roles):
                    role_badge = role_name
                    break

        # Resolve theme
        try:
            from flow_wallet import get_theme_for_render
            theme_id = get_theme_for_render(user.id)
        except Exception:
            theme_id = None

        # Gather data in executor
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, gather_home_data, user.id)

        # Override display name and role badge
        data["display_name"] = user.display_name
        data["role_badge"] = role_badge

        # Theme display name
        if theme_id and theme_id in THEMES:
            data["theme_name"] = THEMES[theme_id].get("name", theme_id)

        # Season from data_manager if available
        try:
            import data_manager as dm
            data["season"] = dm.CURRENT_SEASON
        except Exception:
            pass

        disc_file = await render_home_card_to_file(data, theme_id=theme_id, filename="atlas_home.png")
        view = HomeView(user_id=user.id, data=data, theme_id=theme_id)
        await interaction.followup.send(file=disc_file, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(AtlasHomeCog(bot))
        print("ATLAS: Home · User baseball card loaded. 🏠")
    except Exception as e:
        print(f"ATLAS: Home · FAILED to load ({e})")
```

- [ ] **Step 2: Add `atlas_home_cog` to bot.py load order**

In `_EXTENSIONS` list, add after `"god_cog"`:

```python
        "atlas_home_cog",     # ATLAS Home — user baseball card (/atlas)
```

- [ ] **Step 3: Check THEMES dict has a `name` key**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "
from atlas_themes import THEMES
for k, v in list(THEMES.items())[:3]:
    print(k, list(v.keys())[:4])
"
```

If `THEMES[theme_id]` doesn't have a `"name"` key, adjust `data["theme_name"] = THEMES[theme_id].get("name", theme_id)` to use the correct key or fall back to `theme_id`.

- [ ] **Step 4: Import check**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "import atlas_home_cog; print('atlas_home_cog OK')"
```

Expected: `atlas_home_cog OK`.

- [ ] **Step 5: Commit**

```bash
git add atlas_home_cog.py bot.py
git commit -m "feat: add atlas_home_cog — /atlas user home card with theme cycling"
```

---

## Task 8: Scheduled Task Audit Updates + Final Version Bump + Push

**Files:**
- Modify: Saturday core audit SKILL.md (covers bot.py, boss_cog.py, permissions.py)
- Modify: Wednesday oracle audit SKILL.md (covers oracle_renderer.py)
- Modify: `bot.py` (confirm final version is `7.6.0`)

Per CLAUDE.md: *"When creating a new .py module, add it to the relevant nightly audit task."*

New files map to audit days:
- `atlas_home_cog.py`, `atlas_home_renderer.py` → **Saturday** (Core Infrastructure)
- `god_cog.py` → **Thursday** (Genesis/Sentinel — closest match for admin modules)

- [ ] **Step 1: Update Wednesday oracle SKILL.md**

In `/c/Users/natew/.claude/scheduled-tasks/audit-wednesday-oracle/SKILL.md`:

Add `oracle_renderer.py` to the Primary files list. (It may already be there; verify and add if not.)

- [ ] **Step 2: Update Saturday core SKILL.md + task description**

In `/c/Users/natew/.claude/scheduled-tasks/audit-saturday-core/SKILL.md`:

Add to Primary files list:
```
- `atlas_home_cog.py`
- `atlas_home_renderer.py`
```

Then use the `mcp__scheduled-tasks__update_scheduled_task` MCP tool (available in Claude Code) to update the task's `description` field so the scheduled runner knows to include these files. The description should mention `atlas_home_cog.py` and `atlas_home_renderer.py`.

- [ ] **Step 3: Update Thursday genesis SKILL.md + task description**

In `/c/Users/natew/.claude/scheduled-tasks/audit-thursday-genesis/SKILL.md`:

Add `god_cog.py` to Primary files list.

Then use `mcp__scheduled-tasks__update_scheduled_task` to update the Thursday task's `description` to include `god_cog.py`.

- [ ] **Step 4: Verify final `ATLAS_VERSION`**

Confirm `bot.py` line 175 reads `ATLAS_VERSION = "7.6.0"`. If any additional commits have been made on this branch since Task 5, confirm no accidental revert.

- [ ] **Step 5: Full import smoke test**

```bash
cd C:\Users\natew\Desktop\discord_bot
python -c "
import permissions, boss_cog, god_cog, echo_cog, oracle_renderer
import atlas_home_renderer, atlas_home_cog
print('All imports OK')
"
```

Expected: `All imports OK`.

- [ ] **Step 6: Final render test**

```bash
python test_all_renders.py
```

Expected: All cards pass including `atlas_home`.

- [ ] **Step 7: Final commit + push**

```bash
git add -p  # stage any unstaged changes
git commit -m "chore: update audit task file lists for new modules"
git push
```

---

## Dependency Order Summary

```
Task 1 (oracle theme)   — independent, do first
Task 2 (permissions)    — independent, do second
Task 3 (boss_cog)       — depends on Task 2 (is_commissioner already imported)
Task 4 (god_cog)        — depends on Task 2 (requires is_god/require_god)
Task 5 (bot.py cleanup) — depends on Tasks 3 & 4 (verify impls moved before deleting)
Task 6 (renderer)       — independent of Tasks 1-5
Task 7 (home cog)       — depends on Task 6, and Task 5 (atlas_group must be gone)
Task 8 (audits)         — depends on all above
```

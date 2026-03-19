"""
setup_cog.py — ATLAS Server Initialization
=============================================
Fires on_guild_join to:
  1. Create the server_config table in tsl_history.db
  2. Scan existing channels against the required manifest
  3. Create any missing channels with correct permissions
  4. Store all channel IDs by static Discord ID (never by name)
  5. Post a setup receipt embed in #admin-chat

Other cogs should call get_channel_id(key) to resolve channel targets.

Register in bot.py setup_hook():
    await bot.load_extension("setup_cog")
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from atlas_colors import AtlasColors

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")

# Every channel the bot needs, in order.
# Keys become the config_key stored in server_config.
# Structure: (config_key, display_name, category_name, read_only_for_members, admin_only)
REQUIRED_CHANNELS: list[tuple[str, str, str, bool, bool]] = [
    # ── ATLAS — Command Center (admin + AI) ──
    ("admin_chat",      "admin-chat",      "ATLAS — Command Center", False, True),
    ("bot_logs",        "bot-logs",        "ATLAS — Command Center", True,  True),
    ("ask_atlas",       "ask-atlas",       "ATLAS — Command Center", False, False),
    # ── ATLAS — Oracle (stats, rankings, results) ──
    ("power_rankings",  "power-rankings",  "ATLAS — Oracle", True,  False),
    ("announcements",   "announcements",   "ATLAS — Oracle", False, False),
    ("game_results",    "game-results",    "ATLAS — Oracle", False, False),
    # ── ATLAS — Genesis (roster, trades, development) ──
    ("roster_moves",    "roster-moves",    "ATLAS — Genesis", False, False),
    ("trades",          "trades",          "ATLAS — Genesis", False, False),
    ("dev_upgrades",    "dev-upgrades",    "ATLAS — Genesis", False, False),
    # ── ATLAS — Sentinel (enforcement, compliance) ──
    ("compliance",      "compliance",      "ATLAS — Sentinel", True,  False),
    ("force_request",   "force-request",   "ATLAS — Sentinel", False, False),
    # ── ATLAS — Casino (economy, games, markets) ──
    ("ledger",             "ledger",              "ATLAS — Flow",   True,  False),
    # ── ATLAS — Flow (live engagement feed) ──
    ("flow_live",          "flow-live",           "ATLAS — Flow",   True,  False),
    ("blackjack",          "blackjack",           "ATLAS — Casino", False, False),
    ("slots",              "slots",               "ATLAS — Casino", False, False),
    ("crash",              "crash",               "ATLAS — Casino", False, False),
    ("coinflip",           "coinflip",            "ATLAS — Casino", False, False),
    ("sportsbook",         "sportsbook",          "ATLAS — Casino", False, False),
    ("prediction_markets", "prediction-markets",  "ATLAS — Casino", False, False),
]

# Legacy channel name aliases for remap (old name → config_key)
_CHANNEL_ALIASES: dict[str, str] = {
    "askwittgpt": "ask_atlas",
    "real_sportsbook": "sportsbook",    # merged in v2.1
    "real-sportsbook": "sportsbook",    # display name variant
    "casino_ledger": "ledger",          # renamed in v2.3
    "casino-ledger": "ledger",          # display name variant
}

# Channels where /complaint and /forcerequest are silently routed to DM → admin-chat.
# These command names are enforced in their respective cogs using get_channel_id().
PRIVATE_ROUTING_COMMANDS = {"complaint", "forcerequest"}

# Maps setup_cog config keys to casino_db setting names for channel sync.
_CASINO_BRIDGE = {
    "blackjack": "casino_blackjack_channel",
    "slots":     "casino_slots_channel",
    "crash":     "casino_crash_channel",
    "coinflip":  "casino_coinflip_channel",
}

# ── DB Helpers ────────────────────────────────────────────────────────────────

# In-memory cache for channel IDs — avoids repeated DB hits for values
# that change only when a channel is explicitly reconfigured.
_channel_cache: dict[str, int] = {}


def _ensure_table() -> None:
    """Create server_config table if it doesn't exist. Also enables WAL."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                config_key  TEXT PRIMARY KEY,
                channel_id  INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL
            )
        """)
        con.commit()
    finally:
        con.close()


def get_channel_id(key: str, guild_id: Optional[int] = None) -> Optional[int]:
    """
    Retrieve a stored channel ID by config_key.
    Other cogs import and call this to resolve routing targets.

    Usage:
        from setup_cog import get_channel_id
        ch_id = get_channel_id("admin_chat")
        channel = bot.get_channel(ch_id)
    """
    cache_key = f"{key}:{guild_id or 0}"
    cached = _channel_cache.get(cache_key)
    if cached is not None:
        return cached
    con = None
    try:
        # Short timeout (2s) — fail fast during startup when sync_tsl_db holds
        # a write lock.  Once auto_discover populates the cache, this DB path
        # is rarely hit.
        con = sqlite3.connect(DB_PATH, timeout=2)
        # Ensure table exists — sync_tsl_db's atomic swap can destroy it,
        # and the table preservation step may not always succeed.
        con.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                config_key  TEXT PRIMARY KEY,
                channel_id  INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL
            )
        """)
        if guild_id:
            row = con.execute(
                "SELECT channel_id FROM server_config WHERE config_key=? AND guild_id=?",
                (key, guild_id)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT channel_id FROM server_config WHERE config_key=? LIMIT 1",
                (key,)
            ).fetchone()
        result = row[0] if row else None
        if result is not None:
            _channel_cache[cache_key] = result
        return result
    except Exception:
        log.error("Failed to read channel config for key=%s guild_id=%s", key, guild_id, exc_info=True)
        return None
    finally:
        if con:
            con.close()


async def get_channel_id_async(key: str, guild_id: Optional[int] = None) -> Optional[int]:
    """Async-safe wrapper — checks cache first, then uses executor for DB fallback."""
    cache_key = f"{key}:{guild_id or 0}"
    cached = _channel_cache.get(cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_channel_id, key, guild_id)


def _save_channel_id(key: str, channel_id: int, guild_id: int) -> None:
    _ensure_table()
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        con.execute("""
            INSERT INTO server_config (config_key, channel_id, guild_id)
            VALUES (?, ?, ?)
            ON CONFLICT(config_key) DO UPDATE SET channel_id=excluded.channel_id,
                                                   guild_id=excluded.guild_id
        """, (key, channel_id, guild_id))
        con.commit()
    finally:
        con.close()
    # Invalidate cache for this key
    _channel_cache.pop(f"{key}:{guild_id}", None)


def _clear_guild_config(guild_id: int) -> int:
    """Delete all server_config rows for a guild. Returns count deleted."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        cur = con.execute("DELETE FROM server_config WHERE guild_id=?", (guild_id,))
        con.commit()
        count = cur.rowcount
    finally:
        con.close()
    # Invalidate all cached entries for this guild
    stale = [k for k in _channel_cache if k.endswith(f":{guild_id}")]
    for k in stale:
        _channel_cache.pop(k, None)
    return count


# ── Permission Builders ───────────────────────────────────────────────────────

def _build_overwrites(
    guild: discord.Guild,
    admin_role: Optional[discord.Role],
    read_only: bool,
    admin_only: bool,
) -> dict:
    """
    Build permission overwrites dict for a channel.

    admin_only  → only admin role + bot can see/use
    read_only   → everyone can read, only bot can send messages
    default     → everyone can read and send
    """
    overwrites = {}

    everyone = guild.default_role

    if admin_only:
        # Hide from @everyone entirely
        overwrites[everyone] = discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False
        )
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
        # Bot itself always gets full access
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True
            )
    elif read_only:
        # Everyone sees it, only bot sends
        overwrites[everyone] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True
        )
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True
            )
    else:
        # Default: everyone R/W (Discord default — explicit for clarity)
        overwrites[everyone] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )

    return overwrites


# ── Core Provisioning Logic ───────────────────────────────────────────────────

async def _provision_channels(guild: discord.Guild) -> dict:
    """
    Main provisioning function. Returns a results dict for the receipt embed.
    """
    _ensure_table()
    print(f"[SETUP] Provisioning started for guild '{guild.name}' ({guild.id})")

    results = {
        "found":   [],   # (key, channel)  — already existed
        "created": [],   # (key, channel)  — newly created
        "failed":  [],   # (key, reason)   — couldn't create
    }

    # Build a name → channel lookup (lowercased for fuzzy matching)
    existing: dict[str, discord.TextChannel] = {}
    for ch in guild.text_channels:
        key = ch.name.lower()
        if key in existing:
            log.warning("Channel name collision: '%s' matches both #%s (%s) and #%s (%s)",
                        key, existing[key].name, existing[key].id, ch.name, ch.id)
        existing[key] = ch

    # Build a category name → category object lookup
    categories: dict[str, discord.CategoryChannel] = {
        cat.name.upper(): cat
        for cat in guild.categories
    }

    # Find or identify the Admin role (flexible naming)
    admin_role: Optional[discord.Role] = None
    for role in guild.roles:
        if role.name.lower() in ("admin", "commissioner", "admins", "mod", "moderator"):
            admin_role = role
            break

    for config_key, channel_name, category_name, read_only, admin_only in REQUIRED_CHANNELS:
        existing_ch = existing.get(channel_name.lower())

        if existing_ch:
            # Channel exists — just store the ID
            _save_channel_id(config_key, existing_ch.id, guild.id)
            results["found"].append((config_key, existing_ch))
            print(f"[SETUP]   Found existing: {config_key} -> #{existing_ch.name} ({existing_ch.id})")
        else:
            # Need to create it
            try:
                category = categories.get(category_name.upper())

                # If the target category doesn't exist, create it
                if category is None:
                    print(f"[SETUP]   Creating category: {category_name}")
                    category = await guild.create_category(category_name)
                    categories[category_name.upper()] = category  # cache for reuse

                overwrites = _build_overwrites(guild, admin_role, read_only, admin_only)

                print(f"[SETUP]   Creating channel: #{channel_name} under '{category_name}'")
                new_ch = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason="ATLAS setup"
                )

                _save_channel_id(config_key, new_ch.id, guild.id)
                results["created"].append((config_key, new_ch))

            except discord.Forbidden:
                results["failed"].append((config_key, "Missing Manage Channels permission"))
                print(f"[SETUP]   FAILED: {config_key} — Missing Manage Channels permission")
            except Exception as e:
                results["failed"].append((config_key, str(e)))
                print(f"[SETUP]   FAILED: {config_key} — {e}")

    # ── Migration: remove orphaned real_sportsbook config (one-time) ─────
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as con:
            row = con.execute(
                "SELECT channel_id FROM server_config WHERE config_key='_migration_v2'"
            ).fetchone()
            if not row:
                deleted = con.execute(
                    "DELETE FROM server_config WHERE config_key = 'real_sportsbook'"
                ).rowcount
                if deleted:
                    print(f"[SETUP]   Migration: removed orphaned real_sportsbook config entry")
                con.execute(
                    "INSERT INTO server_config (config_key, channel_id, guild_id) VALUES ('_migration_v2', 0, 0)"
                )
                con.commit()
    except Exception as e:
        print(f"[SETUP]   Migration cleanup note: {e}")

    # ── Bridge casino per-game channel IDs to casino_db ─────────────────
    try:
        from casino.casino_db import set_setting as _casino_set
        for cfg_key, casino_setting in _CASINO_BRIDGE.items():
            ch_id = get_channel_id(cfg_key, guild.id)
            if ch_id:
                await _casino_set(casino_setting, str(ch_id))
                print(f"[SETUP]   Synced {cfg_key} → casino_db ({casino_setting}={ch_id})")
    except Exception as e:
        print(f"[SETUP]   Casino bridge failed: {e}")

    print(f"[SETUP] Provisioning complete: {len(results['found'])} found, {len(results['created'])} created, {len(results['failed'])} failed")
    return results


async def _post_receipt(guild: discord.Guild, results: dict) -> None:
    """Post the setup receipt embed to #admin-chat."""
    admin_ch_id = get_channel_id("admin_chat", guild.id)
    if not admin_ch_id:
        return

    channel = guild.get_channel(admin_ch_id)
    if not channel:
        return

    embed = discord.Embed(
        title="🤖 ATLAS — Server Setup Complete",
        description=(
            f"Bot joined **{guild.name}** and provisioned all required channels.\n"
            f"All channel IDs are stored in `tsl_history.db → server_config`.\n"
            f"Renaming channels will **not** break routing — IDs are static."
        ),
        color=AtlasColors.SUCCESS
    )

    if results["found"]:
        found_lines = "\n".join(
            f"✅ `{key}` → <#{ch.id}>"
            for key, ch in results["found"]
        )
        embed.add_field(name=f"📋 Found Existing ({len(results['found'])})", value=found_lines, inline=False)

    if results["created"]:
        created_lines = "\n".join(
            f"🆕 `{key}` → <#{ch.id}>"
            for key, ch in results["created"]
        )
        embed.add_field(name=f"🏗️ Created ({len(results['created'])})", value=created_lines, inline=False)

    if results["failed"]:
        failed_lines = "\n".join(
            f"❌ `{key}` — {reason}"
            for key, reason in results["failed"]
        )
        embed.add_field(name=f"⚠️ Failed ({len(results['failed'])})", value=failed_lines, inline=False)
        embed.add_field(
            name="🔧 How to Fix",
            value=(
                "Re-invite the bot with **Manage Channels** and **Manage Roles** permissions, "
                "then run `/setup` to complete provisioning."
            ),
            inline=False
        )

    embed.add_field(
        name="🔒 Routing Notes",
        value=(
            "`/complaint` and `/forcerequest` route via DM → `#admin-chat` silently.\n"
            "`#compliance` and `#power-rankings` are read-only for members (bot posts only).\n"
            "`#admin-chat` and `#bot-logs` are hidden from non-admin members."
        ),
        inline=False
    )

    embed.set_footer(text="Run /setup at any time to re-provision missing channels.")
    await channel.send(embed=embed)


# ── Setup UI Views ───────────────────────────────────────────────────────────

class SetupChoiceView(discord.ui.View):
    """Interactive setup: Remap existing channels or create new ATLAS categories."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Remap Existing Channels", style=discord.ButtonStyle.primary, row=0)
    async def remap_existing(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Scan for existing channels by name and store their IDs."""
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        print(f"[SETUP] Remap button clicked by {interaction.user} in '{guild.name}'")
        results = {"found": [], "missing": []}

        existing: dict[str, discord.TextChannel] = {
            ch.name.lower().replace(" ", "-"): ch for ch in guild.text_channels
        }

        for config_key, channel_name, _cat, _ro, _ao in REQUIRED_CHANNELS:
            match = existing.get(channel_name.lower())
            if not match:
                # Check legacy aliases (e.g. "askwittgpt" → ask_atlas)
                for alias, target_key in _CHANNEL_ALIASES.items():
                    if target_key == config_key:
                        match = existing.get(alias)
                        break
            if match:
                _save_channel_id(config_key, match.id, guild.id)
                results["found"].append((config_key, match))
            else:
                results["missing"].append(config_key)

        embed = discord.Embed(
            title="ATLAS Setup — Channel Remap Complete",
            color=AtlasColors.SUCCESS
        )

        if results["found"]:
            found_text = "\n".join(f"`{k}` → <#{ch.id}>" for k, ch in results["found"])
            embed.add_field(name=f"Mapped ({len(results['found'])})", value=found_text, inline=False)

        if results["missing"]:
            missing_text = "\n".join(f"`{k}` — not found" for k in results["missing"])
            embed.add_field(
                name=f"Not Found ({len(results['missing'])})",
                value=missing_text + "\n\nThese channels will be created if you run setup again with **Create New**.",
                inline=False
            )

        embed.set_footer(text="Channel IDs are stored by ID — renaming channels won't break routing.")
        self.stop()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Create New ATLAS Categories", style=discord.ButtonStyle.secondary, row=0)
    async def create_new(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Create new category structure and provision all channels."""
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        print(f"[SETUP] Create New button clicked by {interaction.user} in '{guild.name}'")

        try:
            results = await _provision_channels(guild)

            embed = discord.Embed(
                title="ATLAS Setup — Full Provisioning Complete",
                color=AtlasColors.SUCCESS
            )

            if results["found"]:
                found_text = "\n".join(f"`{k}` → <#{ch.id}>" for k, ch in results["found"])
                embed.add_field(name=f"Already Existed ({len(results['found'])})", value=found_text, inline=False)

            if results["created"]:
                created_text = "\n".join(f"`{k}` → <#{ch.id}>" for k, ch in results["created"])
                embed.add_field(name=f"Created ({len(results['created'])})", value=created_text, inline=False)

            if results["failed"]:
                failed_text = "\n".join(f"`{k}` — {reason}" for k, reason in results["failed"])
                embed.add_field(name=f"Failed ({len(results['failed'])})", value=failed_text, inline=False)

            embed.set_footer(text="Channel IDs are stored by ID — renaming channels won't break routing.")
            self.stop()
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            self.stop()
            await interaction.followup.send(f"ATLAS Setup failed: `{e}`", ephemeral=True)

    @discord.ui.button(label="Delete ATLAS Channels", style=discord.ButtonStyle.danger, row=1)
    async def nuke_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Delete all WITTGPT and ATLAS categories + their channels."""
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        print(f"[SETUP] Delete button clicked by {interaction.user} in '{guild.name}'")

        deleted = 0
        for cat in list(guild.categories):
            name_upper = cat.name.upper()
            if "WITTGPT" in name_upper or "ATLAS " in name_upper:
                print(f"[SETUP]   Deleting category: '{cat.name}' ({len(cat.channels)} channels)")
                for ch in cat.channels:
                    try:
                        await ch.delete(reason="ATLAS cleanup")
                        deleted += 1
                    except Exception as e:
                        print(f"[SETUP]   Failed to delete #{ch.name}: {e}")
                try:
                    await cat.delete(reason="ATLAS cleanup")
                    deleted += 1
                except Exception as e:
                    print(f"[SETUP]   Failed to delete category '{cat.name}': {e}")

        cleared = _clear_guild_config(guild.id)
        print(f"[SETUP] Cleanup complete: {deleted} Discord objects deleted, {cleared} config rows cleared")

        embed = discord.Embed(
            title="ATLAS Cleanup Complete",
            description=f"Deleted **{deleted}** channels/categories.\nCleared **{cleared}** config entries.\n\nRun `/setup` again to create fresh ATLAS channels.",
            color=AtlasColors.ERROR
        )
        self.stop()
        await interaction.followup.send(embed=embed, ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
#  AUTO-DISCOVERY — runs once per guild at startup
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_discovery_tables() -> None:
    """Create guild_registry, guild_roles, and guild_emojis tables."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS guild_registry (
                guild_id      INTEGER PRIMARY KEY,
                guild_name    TEXT,
                owner_id      INTEGER,
                member_count  INTEGER,
                boost_level   INTEGER,
                icon_url      TEXT,
                banner_url    TEXT,
                discovered_at TEXT,
                updated_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS guild_roles (
                guild_id  INTEGER,
                role_id   INTEGER,
                role_name TEXT,
                color     INTEGER,
                position  INTEGER,
                is_admin  INTEGER,
                PRIMARY KEY (guild_id, role_id)
            );
            CREATE TABLE IF NOT EXISTS guild_emojis (
                guild_id   INTEGER,
                emoji_id   INTEGER,
                emoji_name TEXT,
                animated   INTEGER,
                PRIMARY KEY (guild_id, emoji_id)
            );
        """)
    finally:
        con.close()


# Module-level cache: guild_id → {role_name_lower → role_id}
_role_cache: dict[int, dict[str, int]] = {}


def get_cached_role_id(guild_id: int, role_name: str) -> Optional[int]:
    """Fast role lookup from startup cache. Falls back to None."""
    guild_roles = _role_cache.get(guild_id)
    if guild_roles:
        return guild_roles.get(role_name.lower())
    return None


def _auto_discover_db(guild_data: dict) -> dict:
    """
    Synchronous auto-discovery — all SQLite work runs here via run_in_executor.
    Receives a plain-Python snapshot of guild data (no discord.py objects).
    Returns {"role_cache": dict, "log_lines": list[str], "cfg_rows": list}.
    """
    _ensure_table()
    _ensure_discovery_tables()

    gid = guild_data["gid"]
    now = guild_data["now"]
    log: list[str] = []

    log.append(f"\n🔍 Auto-discovery: {guild_data['guild_name']} (guild {gid})")

    # ── 1. CHANNELS — match by name, skip already-configured keys ────────
    existing_keys: set[str] = set()
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = con.execute(
            "SELECT config_key FROM server_config WHERE guild_id=?", (gid,)
        ).fetchall()
        existing_keys = {r[0] for r in rows}
    finally:
        con.close()

    ch_by_name: dict[str, dict] = {}
    for ch in guild_data["channels"]:
        normalized = ch["name"].lower().replace(" ", "-")
        ch_by_name[normalized] = ch

    matched, skipped = 0, 0
    for config_key, channel_name, _cat, _ro, _ao in REQUIRED_CHANNELS:
        if config_key in existing_keys:
            skipped += 1
            continue
        target = ch_by_name.get(channel_name.lower())
        if not target:
            for alias, alias_key in _CHANNEL_ALIASES.items():
                if alias_key == config_key:
                    target = ch_by_name.get(alias)
                    if target:
                        break
        if target:
            _save_channel_id(config_key, target["id"], gid)
            log.append(f"   ✅ {config_key} → #{target['name']} ({target['id']})")
            matched += 1
        else:
            log.append(f"   ⚠️  {config_key} — no match found")

    total = len(REQUIRED_CHANNELS)
    configured = len(existing_keys) + matched
    log.append(f"   Channels: {configured}/{total} configured ({skipped} kept, {matched} auto-matched)")

    # ── 2. PERMISSION AUDIT — check bot perms in configured channels ─────
    perm_ok, perm_warn = 0, 0
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        cfg_rows = con.execute(
            "SELECT config_key, channel_id FROM server_config WHERE guild_id=?", (gid,)
        ).fetchall()
    finally:
        con.close()

    bot_perms = guild_data["bot_perms"]
    for config_key, channel_id in cfg_rows:
        perms = bot_perms.get(channel_id)
        if not perms:
            continue
        missing = []
        if not perms["send"]:
            missing.append("send_messages")
        if not perms["embed"]:
            missing.append("embed_links")
        if not perms["attach"]:
            missing.append("attach_files")
        if not perms["read"]:
            missing.append("read_messages")
        if missing:
            log.append(f"   ⚠️  #{perms['name']} — missing: {', '.join(missing)}")
            perm_warn += 1
        else:
            perm_ok += 1

    if perm_warn:
        log.append(f"   Permissions: {perm_ok} OK, {perm_warn} warnings")
    else:
        log.append(f"   Permissions: {perm_ok} channels OK")

    # ── 3. ROLES — cache all roles, identify key roles ───────────────────
    key_role_ids: dict[str, int | None] = {"commissioner": None, "tsl owner": None}
    key_role_found: dict[str, bool] = {"commissioner": False, "tsl owner": False}
    role_cache_local: dict[str, int] = {}
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("DELETE FROM guild_roles WHERE guild_id=?", (gid,))
        for role in guild_data["roles"]:
            if role["is_default"]:
                continue
            con.execute(
                "INSERT OR REPLACE INTO guild_roles (guild_id, role_id, role_name, color, position, is_admin) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (gid, role["id"], role["name"], role["color_value"], role["position"],
                 1 if role["is_admin"] else 0),
            )
            role_cache_local[role["name"].lower()] = role["id"]
            if role["name"].lower() in key_role_ids:
                key_role_ids[role["name"].lower()] = role["id"]
                key_role_found[role["name"].lower()] = True
        con.commit()
    finally:
        con.close()

    role_count = len([r for r in guild_data["roles"] if not r["is_default"]])
    key_str = ", ".join(
        f"{name.title()} {'✅' if found else '❌'}"
        for name, found in key_role_found.items()
    )
    log.append(f"   Roles: {role_count} cached ({key_str})")

    # ── 4. GUILD METADATA — persist guild info ───────────────────────────
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("""
            INSERT INTO guild_registry (guild_id, guild_name, owner_id, member_count,
                                        boost_level, icon_url, banner_url, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                guild_name=excluded.guild_name, owner_id=excluded.owner_id,
                member_count=excluded.member_count, boost_level=excluded.boost_level,
                icon_url=excluded.icon_url, banner_url=excluded.banner_url,
                updated_at=excluded.updated_at
        """, (gid, guild_data["guild_name"], guild_data["owner_id"],
              guild_data["human_count"], guild_data["premium_tier"],
              guild_data["icon_url"], guild_data["banner_url"], now, now))
        con.commit()
    finally:
        con.close()

    upload_limits = {0: "25MB", 1: "25MB", 2: "50MB", 3: "100MB"}
    log.append(f"   Boost: Level {guild_data['premium_tier']} ({upload_limits.get(guild_data['premium_tier'], '?')} upload limit)")

    # ── 5. CATEGORIES — log structure ────────────────────────────────────
    if guild_data["categories"]:
        cat_parts = []
        for cat in guild_data["categories"]:
            cat_parts.append(f"{cat['name']} ({cat['channel_count']}ch)")
        log.append(f"   Categories: {', '.join(cat_parts)}")

    # ── 6. MEMBER ROLE ENRICHMENT — batched single connection ────────────
    enriched = 0
    commissioner_role_id = key_role_ids.get("commissioner")
    owner_role_id = key_role_ids.get("tsl owner")
    con = None
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        for member in guild_data["members"]:
            if member["bot"]:
                continue
            new_status = None
            if commissioner_role_id and commissioner_role_id in member["role_ids"]:
                new_status = "Admin"
            elif owner_role_id and owner_role_id in member["role_ids"]:
                new_status = "League Owner"
            if new_status:
                cur = con.execute(
                    "UPDATE tsl_members SET status=? WHERE discord_id=? AND status != ?",
                    (new_status, str(member["id"]), new_status),
                )
                if cur.rowcount:
                    enriched += 1
        con.commit()
    except Exception:
        pass  # tsl_members table may not exist yet
    finally:
        if con:
            con.close()

    if enriched:
        log.append(f"   Members: {enriched} status updates from roles")

    # ── 7. EMOJIS — cache custom emojis ──────────────────────────────────
    emoji_count = 0
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.execute("DELETE FROM guild_emojis WHERE guild_id=?", (gid,))
        for emoji in guild_data["emojis"]:
            con.execute(
                "INSERT INTO guild_emojis (guild_id, emoji_id, emoji_name, animated) "
                "VALUES (?, ?, ?, ?)",
                (gid, emoji["id"], emoji["name"], 1 if emoji["animated"] else 0),
            )
            emoji_count += 1
        con.commit()
    finally:
        con.close()

    if emoji_count:
        log.append(f"   Emojis: {emoji_count} custom emojis cached")

    return {
        "role_cache": role_cache_local,
        "log_lines": log,
        "cfg_rows": cfg_rows,
    }


async def auto_discover(guild: discord.Guild) -> None:
    """
    Scan a guild at startup and persist structure to DB.
    Called from on_ready() for each guild. Never overwrites manual /setup config.

    All synchronous SQLite work is offloaded to a thread executor via
    _auto_discover_db() to avoid blocking the Discord gateway heartbeat.
    """
    # ── Collect guild snapshot (CPU-only, no I/O) ────────────────────────
    me = guild.me
    guild_data = {
        "gid": guild.id,
        "guild_name": guild.name,
        "owner_id": guild.owner_id,
        "premium_tier": guild.premium_tier,
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "banner_url": str(guild.banner.url) if guild.banner else None,
        "human_count": sum(1 for m in guild.members if not m.bot),
        "now": datetime.now(timezone.utc).isoformat(),
        "channels": [
            {"name": ch.name, "id": ch.id}
            for ch in guild.text_channels
        ],
        "roles": [
            {
                "name": r.name, "id": r.id, "color_value": r.color.value,
                "position": r.position, "is_admin": r.permissions.administrator,
                "is_default": r.is_default(),
            }
            for r in guild.roles
        ],
        "members": [
            {"id": m.id, "bot": m.bot, "role_ids": {r.id for r in m.roles}}
            for m in guild.members
        ],
        "emojis": [
            {"id": e.id, "name": e.name, "animated": e.animated}
            for e in guild.emojis
        ],
        "categories": sorted(
            [{"name": c.name, "channel_count": len(c.channels), "position": c.position}
             for c in guild.categories],
            key=lambda c: c["position"],
        ) if guild.categories else [],
        "bot_perms": {
            ch.id: {
                "name": ch.name,
                "send": ch.permissions_for(me).send_messages,
                "embed": ch.permissions_for(me).embed_links,
                "attach": ch.permissions_for(me).attach_files,
                "read": ch.permissions_for(me).read_messages,
            }
            for ch in guild.text_channels
        },
    }

    # ── Run all SQLite in thread pool (no event loop blocking) ───────────
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _auto_discover_db, guild_data)

    # ── Update module-level caches ───────────────────────────────────────
    _role_cache[guild.id] = result["role_cache"]

    # ── Print accumulated log lines ──────────────────────────────────────
    for line in result["log_lines"]:
        print(line)

    # ── Casino bridge (async — uses aiosqlite, must stay on event loop) ──
    try:
        from casino.casino_db import set_setting as _casino_set
        for cfg_key, casino_setting in _CASINO_BRIDGE.items():
            ch_id = get_channel_id(cfg_key, guild.id)
            if ch_id:
                await _casino_set(casino_setting, str(ch_id))
    except Exception:
        pass  # Casino module may not be loaded yet

    # ── Webhooks (async — Discord API) ───────────────────────────────────
    webhook_count = 0
    try:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.manage_webhooks:
                hooks = await ch.webhooks()
                webhook_count += len(hooks)
    except Exception:
        pass  # Not critical

    if webhook_count:
        print(f"   Webhooks: {webhook_count} found across accessible channels")

    print(f"   Discovery complete.")


# ── Cog ───────────────────────────────────────────────────────────────────────

class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_table()
        print("ATLAS: Setup · Channel Router loaded.")

    # ── /setup (admin only) ──────────────────────────────────────────────────

    @app_commands.command(
        name="setup",
        description="[Admin] Configure ATLAS channel routing for this server."
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_command(self, interaction: discord.Interaction):
        """Admin only. Presents a choice between remapping existing channels
        or creating new ATLAS category structure."""
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="ATLAS Server Setup",
            description=(
                "Choose how to configure ATLAS channels:\n\n"
                "**Remap Existing** — Scans your server for channels matching "
                "the required names and stores their IDs. No channels are created.\n\n"
                "**Create New** — Creates any missing channels under ATLAS categories "
                "with proper permissions. Existing channels are kept."
            ),
            color=AtlasColors.TSL_GOLD
        )
        embed.set_footer(text="Channel IDs are stored statically — renaming channels won't break routing.")

        view = SetupChoiceView(self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── on_guild_join listener ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        print(f"ATLAS: Joined guild '{guild.name}' ({guild.id})")
        print(f"ATLAS: [on_guild_join] No auto-provisioning. Run /setup to configure channels.")


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

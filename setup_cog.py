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

import logging
import os
import sqlite3
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

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

def _ensure_table() -> None:
    """Create server_config table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                config_key  TEXT PRIMARY KEY,
                channel_id  INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL
            )
        """)
        con.commit()


def get_channel_id(key: str, guild_id: Optional[int] = None) -> Optional[int]:
    """
    Retrieve a stored channel ID by config_key.
    Other cogs import and call this to resolve routing targets.

    Usage:
        from setup_cog import get_channel_id
        ch_id = get_channel_id("admin_chat")
        channel = bot.get_channel(ch_id)
    """
    try:
        with sqlite3.connect(DB_PATH) as con:
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
            return row[0] if row else None
    except Exception:
        log.error("Failed to read channel config for key=%s guild_id=%s", key, guild_id, exc_info=True)
        return None


def _save_channel_id(key: str, channel_id: int, guild_id: int) -> None:
    _ensure_table()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO server_config (config_key, channel_id, guild_id)
            VALUES (?, ?, ?)
            ON CONFLICT(config_key) DO UPDATE SET channel_id=excluded.channel_id,
                                                   guild_id=excluded.guild_id
        """, (key, channel_id, guild_id))
        con.commit()


def _clear_guild_config(guild_id: int) -> int:
    """Delete all server_config rows for a guild. Returns count deleted."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM server_config WHERE guild_id=?", (guild_id,))
        con.commit()
        return cur.rowcount


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

    # ── Migration: remove orphaned real_sportsbook config ─────────────────
    try:
        with sqlite3.connect(DB_PATH) as con:
            deleted = con.execute(
                "DELETE FROM server_config WHERE config_key = 'real_sportsbook'"
            ).rowcount
            if deleted:
                con.commit()
                print(f"[SETUP]   Migration: removed orphaned real_sportsbook config entry")
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
        color=0x00C851
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
            color=0x00C851
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
                color=0x00C851
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
            color=0xFF4444
        )
        self.stop()
        await interaction.followup.send(embed=embed, ephemeral=True)


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
            color=0xC9962A
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

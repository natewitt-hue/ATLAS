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

import os
import sqlite3
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")

# Every channel the bot needs, in order.
# Keys become the config_key stored in server_config.
# Structure: (config_key, display_name, category_name, read_only_for_members, admin_only)
REQUIRED_CHANNELS: list[tuple[str, str, str, bool, bool]] = [
    # WITTGPT category
    ("admin_chat",      "admin-chat",      "WITTGPT", False, True),   # admin-only R/W
    ("bot_logs",        "bot-logs",        "WITTGPT", True,  True),   # admin-only, bot posts only
    ("askwittgpt",      "askwittgpt",      "WITTGPT", False, False),  # everyone R/W
    ("compliance",      "compliance",      "WITTGPT", True,  False),  # everyone reads, bot posts
    ("power_rankings",  "power-rankings",  "WITTGPT", True,  False),  # everyone reads, bot posts
    ("sportsbook",      "sportsbook",      "WITTGPT", False, False),  # everyone R/W
    ("casino",          "casino",          "WITTGPT", False, False),  # everyone R/W
    # TSL LEAGUE UPDATES category (existing — bot just needs the IDs)
    ("announcements",   "announcements",   "TSL LEAGUE UPDATES", False, False),
    ("game_results",    "game-results",    "TSL LEAGUE UPDATES", False, False),
    ("roster_moves",    "roster-moves",    "TSL LEAGUE UPDATES", False, False),
    ("dev_upgrades",    "dev-upgrades",    "TSL LEAGUE UPDATES", False, False),
    # TSL TRADE CENTER category (existing)
    ("trades",          "trades",          "TSL TRADE CENTER", False, False),
    # TSL MADDEN category (existing — for force-request routing)
    ("force_request",   "force-request",   "TSL MADDEN", False, False),
    # Prediction Markets channel (used by polymarket_cog)
    ("prediction_markets", "prediction-markets", "WITTGPT", False, False),
]

# Channels where /complaint and /forcerequest are silently routed to DM → admin-chat.
# These command names are enforced in their respective cogs using get_channel_id().
PRIVATE_ROUTING_COMMANDS = {"complaint", "forcerequest"}

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
        return None


def _save_channel_id(key: str, channel_id: int, guild_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO server_config (config_key, channel_id, guild_id)
            VALUES (?, ?, ?)
            ON CONFLICT(config_key) DO UPDATE SET channel_id=excluded.channel_id,
                                                   guild_id=excluded.guild_id
        """, (key, channel_id, guild_id))
        con.commit()


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

    results = {
        "found":   [],   # (key, channel)  — already existed
        "created": [],   # (key, channel)  — newly created
        "failed":  [],   # (key, reason)   — couldn't create
    }

    # Build a name → channel lookup (lowercased for fuzzy matching)
    existing: dict[str, discord.TextChannel] = {
        ch.name.lower(): ch
        for ch in guild.text_channels
    }

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
        else:
            # Need to create it
            try:
                category = categories.get(category_name.upper())

                # If the target category doesn't exist, create it
                if category is None:
                    category = await guild.create_category(category_name)

                overwrites = _build_overwrites(guild, admin_role, read_only, admin_only)

                new_ch = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason="ATLAS auto-setup on join"
                )

                _save_channel_id(config_key, new_ch.id, guild.id)
                results["created"].append((config_key, new_ch))

            except discord.Forbidden:
                results["failed"].append((config_key, "Missing Manage Channels permission"))
            except Exception as e:
                results["failed"].append((config_key, str(e)))

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
        results = {"found": [], "missing": []}

        existing: dict[str, discord.TextChannel] = {
            ch.name.lower().replace(" ", "-"): ch for ch in guild.text_channels
        }

        for config_key, channel_name, _cat, _ro, _ao in REQUIRED_CHANNELS:
            match = existing.get(channel_name.lower())
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
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── on_guild_join listener ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        print(f"ATLAS: Joined guild '{guild.name}' ({guild.id}) — provisioning channels...")
        try:
            results = await _provision_channels(guild)
            await _post_receipt(guild, results)
            print(
                f"ATLAS: Setup complete — "
                f"{len(results['found'])} found, "
                f"{len(results['created'])} created, "
                f"{len(results['failed'])} failed."
            )
        except Exception:
            traceback.print_exc()
            try:
                owner = guild.owner
                if owner:
                    await owner.send(
                        f"**ATLAS Setup Failed** on `{guild.name}`.\n"
                        f"Please re-invite the bot with **Manage Channels** and **Manage Roles** permissions, "
                        f"then run `/setup` to complete initialization."
                    )
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
